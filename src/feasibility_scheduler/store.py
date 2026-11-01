"""SQLite 持久化的任务状态、执行租约与幂等键（规格第 4.2、4.3 节）。

职责边界
--------
- **状态**：所有状态变化都经 :mod:`feasibility_scheduler.statemachine` 的迁移表
  校验，非法迁移在这里同样被拒绝且不落库。
- **租约**：``claim()`` 是唯一合法的“领取”入口，领取时写入 worker 与到期时间；
  ``renew_lease()`` 只能由持有者续约；``expire_leases()`` 把过期租约统一迁移到
  ``RECOVERY_REQUIRED``，不会回到 ``READY``。
- **幂等键**：每个幂等键只能成功领取一次。键写入 ``claims`` 表并由主键约束兜底，
  重复使用同一个键（同一任务或别的任务）都会抛 ``DuplicateClaimError``，即使
  原来的租约已经过期、任务已经进入终态也不回收该键。
- **事件日志**：每次领取、状态迁移、租约过期都追加一条事件，按 ``sequence``
  排序，可用于状态/事件一致性核对。

时间统一按 UTC 存储为 ISO 8601 字符串；测试可注入 ``clock`` 返回带时区的
``datetime``。所有写入使用 ``BEGIN IMMEDIATE``，进程内再用 ``RLock`` 串行化。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import statemachine
from .statemachine import TaskState

DEFAULT_LEASE_SECONDS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    lease_owner       TEXT,
    lease_expires_at  TEXT,
    idempotency_key   TEXT,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    idempotency_key TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    worker_id       TEXT NOT NULL,
    claimed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""


class StoreError(Exception):
    """存储层错误的基类。"""


class TaskNotFoundError(StoreError):
    pass


class DuplicateTaskError(StoreError):
    pass


class ClaimRejectedError(StoreError):
    """当前状态不允许领取。"""


class DuplicateClaimError(StoreError):
    """幂等键已被使用：同一键不能再次领取。"""

    def __init__(self, idempotency_key: str, owner_task_id: str) -> None:
        self.idempotency_key = idempotency_key
        self.owner_task_id = owner_task_id
        super().__init__(
            f"幂等键 {idempotency_key!r} 已被任务 {owner_task_id!r} 的领取占用，拒绝重复领取"
        )


class LeaseConflictError(StoreError):
    """租约不存在、已过期或不属于该 worker。"""


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    state: TaskState
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    idempotency_key: str | None
    updated_at: datetime

    def lease_active(self, now: datetime) -> bool:
        """租约是否存在且未到期；``now`` 必须带时区。"""
        if self.lease_owner is None or self.lease_expires_at is None:
            return False
        return _utc(now) < self.lease_expires_at


@dataclass(frozen=True)
class Event:
    sequence: int
    task_id: str
    from_state: TaskState | None
    to_state: TaskState
    reason: str
    idempotency_key: str | None
    at: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise StoreError("时间必须带时区，避免本地时间歧义")
    return value.astimezone(timezone.utc)


def _format(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class TaskStore:
    """任务状态、租约与幂等键的 SQLite 存储。"""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or _default_clock
        self._lock = threading.RLock()
        if isinstance(path, Path) and path != Path(":memory:"):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        # isolation_level=None：事务完全由 _transaction() 的 BEGIN IMMEDIATE 控制，
        # 避免 sqlite3 隐式事务与显式 BEGIN 冲突。
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------- 生命周期

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _now(self, now: datetime | None) -> datetime:
        return _utc(now) if now is not None else _utc(self._clock())

    # ------------------------------------------------------------- 读操作

    def get_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"任务不存在：{task_id!r}")
        return _row_to_record(row)

    def list_tasks(self, state: TaskState | str | None = None) -> tuple[TaskRecord, ...]:
        query = "SELECT * FROM tasks"
        params: tuple[object, ...] = ()
        if state is not None:
            query += " WHERE state = ?"
            params = (statemachine.parse_state(state).value,)
        query += " ORDER BY task_id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def events(self, task_id: str | None = None) -> tuple[Event, ...]:
        with self._lock:
            if task_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM task_events ORDER BY sequence"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM task_events WHERE task_id = ? ORDER BY sequence",
                    (task_id,),
                ).fetchall()
        return tuple(_row_to_event(row) for row in rows)

    def claim_for(self, task_id: str) -> tuple[str, str, datetime] | None:
        """返回任务当前幂等键对应的领取记录 ``(key, worker, at)``。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT idempotency_key, worker_id, claimed_at FROM claims "
                "WHERE task_id = ? ORDER BY claimed_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return row["idempotency_key"], row["worker_id"], _parse(row["claimed_at"])

    # ------------------------------------------------------------- 写操作

    def create_task(
        self,
        task_id: str,
        *,
        state: TaskState | str = TaskState.PENDING,
        now: datetime | None = None,
    ) -> TaskRecord:
        if not task_id:
            raise StoreError("task_id 不能为空")
        timestamp = self._now(now)
        state_value = statemachine.parse_state(state)
        with self._lock, self._transaction():
            try:
                self._conn.execute(
                    "INSERT INTO tasks (task_id, state, attempts, updated_at) "
                    "VALUES (?, ?, 0, ?)",
                    (task_id, state_value.value, _format(timestamp)),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateTaskError(f"任务已存在：{task_id!r}") from exc
            self._append_event(
                task_id,
                None,
                state_value,
                "创建任务",
                None,
                timestamp,
            )
        return self.get_task(task_id)

    def transition(
        self,
        task_id: str,
        target: TaskState | str,
        *,
        reason: str = "",
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> TaskRecord:
        """按迁移表推进状态；``CLAIMED`` 必须走 :meth:`claim`。"""
        target_state = statemachine.parse_state(target)
        if target_state is TaskState.CLAIMED:
            raise StoreError("领取 CLAIMED 必须通过 claim()，以写入租约与幂等键")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            record = self._require(task_id)
            transition = statemachine.check_transition(record.state, target_state)
            self._apply_state(
                record,
                target_state,
                reason or transition.reason,
                idempotency_key,
                timestamp,
                clear_lease=target_state not in statemachine.LEASE_HOLDING_STATES,
            )
        return self.get_task(task_id)

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        idempotency_key: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> TaskRecord:
        """领取任务：写入租约、递增尝试次数、消费幂等键。

        同一个 ``idempotency_key`` 第二次出现（无论哪个任务）都会被拒绝。
        """
        if not worker_id:
            raise StoreError("worker_id 不能为空")
        if not idempotency_key:
            raise StoreError("idempotency_key 不能为空")
        if lease_seconds <= 0:
            raise StoreError("lease_seconds 必须大于 0")
        timestamp = self._now(now)
        expires_at = timestamp + timedelta(seconds=lease_seconds)

        with self._lock, self._transaction():
            record = self._require(task_id)
            used = self._conn.execute(
                "SELECT task_id FROM claims WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if used is not None:
                raise DuplicateClaimError(idempotency_key, used["task_id"])
            if record.idempotency_key == idempotency_key:
                raise DuplicateClaimError(idempotency_key, record.task_id)
            try:
                statemachine.check_transition(record.state, TaskState.CLAIMED)
            except statemachine.IllegalTransition as exc:
                raise ClaimRejectedError(
                    f"任务 {task_id!r} 当前状态 {record.state.value} 不允许领取"
                ) from exc
            if record.lease_active(timestamp):
                raise LeaseConflictError(
                    f"任务 {task_id!r} 的租约仍被 {record.lease_owner!r} 持有"
                )
            try:
                self._conn.execute(
                    "INSERT INTO claims (idempotency_key, task_id, worker_id, claimed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (idempotency_key, task_id, worker_id, _format(timestamp)),
                )
            except sqlite3.IntegrityError as exc:  # 并发下由主键兜底
                raise DuplicateClaimError(idempotency_key, task_id) from exc
            self._conn.execute(
                "UPDATE tasks SET state = ?, attempts = attempts + 1, "
                "lease_owner = ?, lease_expires_at = ?, idempotency_key = ?, "
                "updated_at = ? WHERE task_id = ?",
                (
                    TaskState.CLAIMED.value,
                    worker_id,
                    _format(expires_at),
                    idempotency_key,
                    _format(timestamp),
                    task_id,
                ),
            )
            self._append_event(
                task_id,
                record.state,
                TaskState.CLAIMED,
                f"Worker {worker_id} 领取任务，租约至 {_format(expires_at)}",
                idempotency_key,
                timestamp,
            )
        return self.get_task(task_id)

    def renew_lease(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> TaskRecord:
        if lease_seconds <= 0:
            raise StoreError("lease_seconds 必须大于 0")
        timestamp = self._now(now)
        with self._lock, self._transaction():
            record = self._require(task_id)
            self._assert_lease_holder(record, worker_id, timestamp)
            expires_at = timestamp + timedelta(seconds=lease_seconds)
            self._conn.execute(
                "UPDATE tasks SET lease_expires_at = ?, updated_at = ? WHERE task_id = ?",
                (_format(expires_at), _format(timestamp), task_id),
            )
        return self.get_task(task_id)

    def release_claim(
        self, task_id: str, worker_id: str, *, now: datetime | None = None
    ) -> TaskRecord:
        """未开始执行的领取可主动释放，任务回到 ``READY``。"""
        timestamp = self._now(now)
        with self._lock, self._transaction():
            record = self._require(task_id)
            self._assert_lease_holder(record, worker_id, timestamp)
            if record.state is not TaskState.CLAIMED:
                raise ClaimRejectedError(
                    f"任务 {task_id!r} 当前状态 {record.state.value} 不能释放领取"
                )
            self._apply_state(
                record,
                TaskState.READY,
                f"Worker {worker_id} 未开始执行即释放领取",
                None,
                timestamp,
                clear_lease=True,
            )
        return self.get_task(task_id)

    def expire_leases(self, *, now: datetime | None = None) -> tuple[TaskRecord, ...]:
        """把所有已过期租约迁移到 ``RECOVERY_REQUIRED`` 并记录事件。"""
        timestamp = self._now(now)
        expired: list[str] = []
        with self._lock, self._transaction():
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE lease_expires_at IS NOT NULL "
                "AND state IN (?, ?) ORDER BY lease_expires_at, task_id",
                (TaskState.CLAIMED.value, TaskState.RUNNING.value),
            ).fetchall()
            for row in rows:
                record = _row_to_record(row)
                if record.lease_expires_at is None or record.lease_expires_at > timestamp:
                    continue
                target = statemachine.transition_on_lease_expiry(record.state)
                owner = record.lease_owner or "未知 worker"
                self._apply_state(
                    record,
                    target,
                    f"租约过期（{owner} 未在 {_format(record.lease_expires_at)} 前续约）",
                    record.idempotency_key,
                    timestamp,
                    clear_lease=True,
                )
                expired.append(record.task_id)
        return tuple(self.get_task(task_id) for task_id in expired)

    # ------------------------------------------------------------- 内部

    def _require(self, task_id: str) -> TaskRecord:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"任务不存在：{task_id!r}")
        return _row_to_record(row)

    def _assert_lease_holder(
        self, record: TaskRecord, worker_id: str, now: datetime
    ) -> None:
        if record.state not in statemachine.LEASE_HOLDING_STATES:
            raise LeaseConflictError(
                f"任务 {record.task_id!r} 当前状态 {record.state.value} 不持有租约"
            )
        if record.lease_owner != worker_id:
            raise LeaseConflictError(
                f"任务 {record.task_id!r} 的租约属于 {record.lease_owner!r}，不是 {worker_id!r}"
            )
        if record.lease_expires_at is None or record.lease_expires_at <= now:
            raise LeaseConflictError(
                f"任务 {record.task_id!r} 的租约已于 {record.lease_expires_at} 过期"
            )

    def _apply_state(
        self,
        record: TaskRecord,
        target: TaskState,
        reason: str,
        idempotency_key: str | None,
        timestamp: datetime,
        *,
        clear_lease: bool,
    ) -> None:
        if clear_lease:
            self._conn.execute(
                "UPDATE tasks SET state = ?, lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE task_id = ?",
                (target.value, _format(timestamp), record.task_id),
            )
        else:
            self._conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (target.value, _format(timestamp), record.task_id),
            )
        self._append_event(
            record.task_id, record.state, target, reason, idempotency_key, timestamp
        )

    def _append_event(
        self,
        task_id: str,
        from_state: TaskState | None,
        to_state: TaskState,
        reason: str,
        idempotency_key: str | None,
        at: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO task_events "
            "(task_id, from_state, to_state, reason, idempotency_key, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_id,
                from_state.value if from_state is not None else None,
                to_state.value,
                reason,
                idempotency_key,
                _format(at),
            ),
        )


def _row_to_record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        state=TaskState(row["state"]),
        attempts=row["attempts"],
        lease_owner=row["lease_owner"],
        lease_expires_at=(
            _parse(row["lease_expires_at"]) if row["lease_expires_at"] else None
        ),
        idempotency_key=row["idempotency_key"],
        updated_at=_parse(row["updated_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        sequence=row["sequence"],
        task_id=row["task_id"],
        from_state=(
            TaskState(row["from_state"]) if row["from_state"] is not None else None
        ),
        to_state=TaskState(row["to_state"]),
        reason=row["reason"],
        idempotency_key=row["idempotency_key"],
        at=_parse(row["at"]),
    )

