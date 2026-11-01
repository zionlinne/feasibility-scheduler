"""追加式 JSONL 事件日志（规格第 7 节）。

每条事件都带齐规格 7 要求的字段：run ID、event ID、timestamp、task /
candidate ID（不适用时为 null）、source revision、config hash、task input
hash、component、previous / new state、decision reason 与 evidence references。
写入采用“一行一个 JSON 对象”的追加模式，每行立即 flush 并 fsync，崩溃后不会
留下半行被悄悄接受：读取端遇到空行、坏 JSON、重复 event ID 或字段缺失会直接
报错。

安全约束
--------
日志永远不写 API key。:class:`Event` 构造时会递归检查键名与字符串值，命中
``api_key``、``authorization``、``sk-...``、``Bearer ...`` 等模式时抛
:class:`SecretLeakError`，并且**不写入任何内容**。usage 里的 ``input_tokens`` /
``output_tokens`` 不视为密钥。

时间统一为带时区的 UTC；``clock`` 可注入，便于测试获得固定时间戳。event ID
由 ``run_id``、序号与事件类型通过 UUIDv5 生成，同一日志位置重放时保持稳定。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .statemachine import TaskState, parse_state

#: 规格第 7 节列出的全部事件类型。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_created",
        "task_validated",
        "phi_evaluated",
        "task_blocked",
        "task_deferred",
        "task_ready",
        "candidate_generated",
        "candidate_validated",
        "candidate_rejected",
        "candidate_selected",
        "graph_rewrite_applied",
        "task_claimed",
        "api_request_started",
        "api_response_received",
        "api_request_failed",
        "retry_scheduled",
        "artifact_created",
        "artifact_validated",
        "collaboration_result_delivered",
        "acceptance_started",
        "acceptance_passed",
        "acceptance_failed",
        "task_completed",
        "run_completed",
        "run_failed",
    }
)

#: 每条事件必须出现的键，顺序即 JSONL 中的展示顺序（实际按 key 排序写出）。
REQUIRED_FIELDS: tuple[str, ...] = (
    "run_id",
    "event_id",
    "sequence",
    "timestamp",
    "type",
    "task_id",
    "candidate_id",
    "source_revision",
    "config_hash",
    "task_input_hash",
    "component",
    "previous_state",
    "new_state",
    "decision_reason",
    "evidence_refs",
    "details",
)

_EVENT_NAMESPACE = uuid.UUID("6f0f4b1e-6a0e-5b2a-9c3d-8f7e6d5c4b3a")

_SECRET_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "secret",
        "password",
        "bearer",
        "token",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "apisecret",
        "credential",
        "credentials",
    }
)

_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password)\s*[:=]\s*\S{8,}"),
)


class EventLogError(Exception):
    """事件日志错误的基类。"""


class InvalidEventError(EventLogError, ValueError):
    """事件字段不合法。"""


class SecretLeakError(InvalidEventError):
    """事件里检测到疑似 API key / 凭证，拒绝写入。"""


class EventLogFormatError(EventLogError):
    """日志文件格式错误（坏行、重复 ID、乱序等）。"""


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise InvalidEventError("timestamp 必须带时区，避免本地时间歧义")
    return value.astimezone(timezone.utc)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat(timespec="microseconds")
    return value


def _is_secret_key(key: str) -> bool:
    normalized = "".join(char for char in key.lower() if char.isalnum())
    if normalized in _SECRET_KEYS:
        return True
    return normalized.endswith(
        ("apikey", "secret", "password", "authorization", "credential")
    )


def _assert_no_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _is_secret_key(key_text):
                raise SecretLeakError(f"事件字段 {path}.{key_text} 疑似凭证，拒绝写入")
            _assert_no_secrets(item, f"{path}.{key_text}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secrets(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                raise SecretLeakError(f"事件字段 {path} 的文本疑似包含凭证，拒绝写入")


def _event_id(run_id: str, sequence: int, event_type: str) -> str:
    return uuid.uuid5(_EVENT_NAMESPACE, f"{run_id}|{sequence}|{event_type}").hex


def hash_config(config: Mapping[str, Any]) -> str:
    """配置哈希：键排序、紧凑 JSON、UTF-8。"""
    if not isinstance(config, Mapping):
        raise InvalidEventError("config 必须是映射")
    payload = json.dumps(
        _plain(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Event:
    """一条不可变事件。``previous_state`` / ``new_state`` 可为空。"""

    run_id: str
    event_id: str
    sequence: int
    timestamp: datetime
    type: str
    component: str
    source_revision: str
    config_hash: str
    task_input_hash: str = ""
    task_id: str | None = None
    candidate_id: str | None = None
    previous_state: TaskState | None = None
    new_state: TaskState | None = None
    decision_reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or self.type not in EVENT_TYPES:
            raise InvalidEventError(f"未知事件类型：{self.type!r}")
        for name in ("run_id", "event_id", "component", "source_revision", "config_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidEventError(f"{name} 不能为空")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise InvalidEventError("sequence 必须是整数")
        if self.sequence < 0:
            raise InvalidEventError("sequence 不能为负")
        if not isinstance(self.task_input_hash, str):
            raise InvalidEventError("task_input_hash 必须是字符串")
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id.strip()
        ):
            raise InvalidEventError("task_id 必须是 null 或非空字符串")
        if self.candidate_id is not None and (
            not isinstance(self.candidate_id, str) or not self.candidate_id.strip()
        ):
            raise InvalidEventError("candidate_id 必须是 null 或非空字符串")
        if not isinstance(self.decision_reason, str):
            raise InvalidEventError("decision_reason 必须是字符串")
        if not isinstance(self.evidence_refs, (list, tuple)) or any(
            not isinstance(item, str) for item in self.evidence_refs
        ):
            raise InvalidEventError("evidence_refs 必须是字符串序列")
        object.__setattr__(self, "timestamp", _ensure_utc(self.timestamp))
        object.__setattr__(self, "previous_state", _optional_state(self.previous_state))
        object.__setattr__(self, "new_state", _optional_state(self.new_state))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "details", _freeze(self.details))
        _assert_no_secrets(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(timespec="microseconds"),
            "type": self.type,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "source_revision": self.source_revision,
            "config_hash": self.config_hash,
            "task_input_hash": self.task_input_hash,
            "component": self.component,
            "previous_state": (
                self.previous_state.value if self.previous_state is not None else None
            ),
            "new_state": self.new_state.value if self.new_state is not None else None,
            "decision_reason": self.decision_reason,
            "evidence_refs": list(self.evidence_refs),
            "details": _plain(self.details),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        if not isinstance(data, Mapping):
            raise InvalidEventError("事件必须是 JSON 对象")
        missing = [name for name in REQUIRED_FIELDS if name not in data]
        if missing:
            raise InvalidEventError("事件缺少字段：" + ", ".join(missing))
        timestamp = data["timestamp"]
        if not isinstance(timestamp, str):
            raise InvalidEventError("timestamp 必须是 ISO 8601 字符串")
        try:
            parsed_time = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise InvalidEventError(f"timestamp 不是合法时间：{timestamp!r}") from exc
        details = data["details"]
        if not isinstance(details, Mapping):
            raise InvalidEventError("details 必须是 JSON 对象")
        evidence = data["evidence_refs"]
        if not isinstance(evidence, (list, tuple)):
            raise InvalidEventError("evidence_refs 必须是数组")
        return cls(
            run_id=data["run_id"],
            event_id=data["event_id"],
            sequence=data["sequence"],
            timestamp=parsed_time,
            type=data["type"],
            component=data["component"],
            source_revision=data["source_revision"],
            config_hash=data["config_hash"],
            task_input_hash=data["task_input_hash"],
            task_id=data["task_id"],
            candidate_id=data["candidate_id"],
            previous_state=data["previous_state"],
            new_state=data["new_state"],
            decision_reason=data["decision_reason"],
            evidence_refs=tuple(evidence),
            details=details,
        )


def _optional_state(value: TaskState | str | None) -> TaskState | None:
    if value is None:
        return None
    try:
        return parse_state(value)
    except ValueError as exc:
        raise InvalidEventError(str(exc)) from exc


def read_events(
    path: str | Path, *, missing_ok: bool = False
) -> tuple[Event, ...]:
    """按行读取并校验事件；任何坏行都报错，绝不静默跳过。"""
    log_path = Path(path)
    if not log_path.exists():
        if missing_ok:
            return ()
        raise EventLogError(f"事件日志不存在：{log_path}")

    events: list[Event] = []
    seen_ids: set[str] = set()
    previous_sequence: int | None = None
    with log_path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                raise EventLogFormatError(f"{log_path} 第 {lineno} 行为空行")
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EventLogFormatError(
                    f"{log_path} 第 {lineno} 行不是合法 JSON：{exc.msg}"
                ) from exc
            if not isinstance(data, dict):
                raise EventLogFormatError(f"{log_path} 第 {lineno} 行不是 JSON 对象")
            try:
                event = Event.from_dict(data)
            except InvalidEventError as exc:
                raise EventLogFormatError(
                    f"{log_path} 第 {lineno} 行事件不合法：{exc}"
                ) from exc
            if event.event_id in seen_ids:
                raise EventLogFormatError(
                    f"{log_path} 第 {lineno} 行 event_id 重复：{event.event_id}"
                )
            if previous_sequence is not None and event.sequence <= previous_sequence:
                raise EventLogFormatError(
                    f"{log_path} 第 {lineno} 行 sequence 未严格递增："
                    f"{event.sequence} <= {previous_sequence}"
                )
            seen_ids.add(event.event_id)
            previous_sequence = event.sequence
            events.append(event)
    return tuple(events)


class EventLog:
    """单个 run 的追加式 JSONL 日志。"""

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        source_revision: str = "local-dev",
        config_hash: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not run_id.strip():
            raise InvalidEventError("run_id 不能为空")
        self.path = Path(path)
        self.run_id = run_id
        self.source_revision = source_revision
        self.config_hash = config_hash if config_hash is not None else hash_config({})
        self._clock = clock or _default_clock
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_events(self.path, missing_ok=True)
        for event in existing:
            if event.run_id != run_id:
                raise EventLogError(
                    f"日志属于 run {event.run_id!r}，不能追加到 run {run_id!r}"
                )
        self._sequence = existing[-1].sequence + 1 if existing else 0
        if not self.path.exists():
            self.path.touch()

    def __len__(self) -> int:
        return len(read_events(self.path, missing_ok=True))

    def record(
        self,
        event_type: str,
        *,
        component: str,
        task_id: str | None = None,
        candidate_id: str | None = None,
        previous_state: TaskState | str | None = None,
        new_state: TaskState | str | None = None,
        decision_reason: str = "",
        evidence_refs: Iterable[str] = (),
        details: Mapping[str, Any] | None = None,
        task_input_hash: str = "",
        source_revision: str | None = None,
        config_hash: str | None = None,
        timestamp: datetime | None = None,
    ) -> Event:
        """追加一条事件；字段或密钥校验失败时不写入。"""
        with self._lock:
            sequence = self._sequence
            event = Event(
                run_id=self.run_id,
                event_id=_event_id(self.run_id, sequence, event_type),
                sequence=sequence,
                timestamp=timestamp or self._clock(),
                type=event_type,
                component=component,
                source_revision=source_revision or self.source_revision,
                config_hash=config_hash or self.config_hash,
                task_input_hash=task_input_hash,
                task_id=task_id,
                candidate_id=candidate_id,
                previous_state=previous_state,
                new_state=new_state,
                decision_reason=decision_reason,
                evidence_refs=tuple(evidence_refs),
                details=details or {},
            )
            self._append(event)
            self._sequence += 1
            return event

    def append(self, event: Event) -> Event:
        """追加已构造的事件；序号必须紧接当前日志末尾。"""
        with self._lock:
            if event.run_id != self.run_id:
                raise EventLogError(
                    f"事件属于 run {event.run_id!r}，日志是 run {self.run_id!r}"
                )
            if event.sequence != self._sequence:
                raise EventLogError(
                    f"事件序号 {event.sequence} 与日志下一序号 {self._sequence} 不一致"
                )
            self._append(event)
            self._sequence += 1
            return event

    def events(self) -> tuple[Event, ...]:
        existing = read_events(self.path)
        for event in existing:
            if event.run_id != self.run_id:
                raise EventLogError(
                    f"日志包含 run {event.run_id!r} 的事件，不属于 run {self.run_id!r}"
                )
        return existing

    def _append(self, event: Event) -> None:
        line = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
