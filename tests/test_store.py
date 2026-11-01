"""store.py 测试：SQLite 持久化、租约、幂等键与状态机联动。"""

from datetime import datetime, timedelta, timezone

import pytest

from feasibility_scheduler.statemachine import IllegalTransition, TaskState, TerminalStateError
from feasibility_scheduler.store import (
    ClaimRejectedError,
    DuplicateClaimError,
    DuplicateTaskError,
    LeaseConflictError,
    StoreError,
    TaskNotFoundError,
    TaskStore,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock) -> TaskStore:
    with TaskStore(tmp_path / "tasks.db", clock=clock) as opened:
        yield opened


def make_ready(store: TaskStore, task_id: str = "t1") -> None:
    store.create_task(task_id)
    store.transition(task_id, TaskState.FEASIBILITY_CHECK)
    store.transition(task_id, TaskState.READY)


# --------------------------------------------------------------- 基础持久化


def test_create_and_read_task(store):
    record = store.create_task("t1")

    assert record.state is TaskState.PENDING
    assert record.attempts == 0
    assert record.lease_owner is None
    assert record.lease_expires_at is None
    assert store.get_task("t1") == record
    events = store.events("t1")
    assert [(event.from_state, event.to_state) for event in events] == [
        (None, TaskState.PENDING)
    ]


def test_duplicate_task_rejected(store):
    store.create_task("t1")
    with pytest.raises(DuplicateTaskError):
        store.create_task("t1")


def test_unknown_task_rejected(store):
    with pytest.raises(TaskNotFoundError):
        store.get_task("missing")
    with pytest.raises(TaskNotFoundError):
        store.transition("missing", TaskState.FEASIBILITY_CHECK)
    with pytest.raises(TaskNotFoundError):
        store.claim("missing", "w1", idempotency_key="k1")


def test_state_survives_reopen(tmp_path, clock):
    path = tmp_path / "tasks.db"
    with TaskStore(path, clock=clock) as first:
        make_ready(first, "t1")

    with TaskStore(path, clock=clock) as second:
        assert second.get_task("t1").state is TaskState.READY
        assert [e.to_state for e in second.events("t1")] == [
            TaskState.PENDING,
            TaskState.FEASIBILITY_CHECK,
            TaskState.READY,
        ]


def test_list_tasks_by_state(store):
    make_ready(store, "ready-one")
    store.create_task("pending-one")
    assert [r.task_id for r in store.list_tasks(TaskState.READY)] == ["ready-one"]
    assert [r.task_id for r in store.list_tasks("PENDING")] == ["pending-one"]
    assert len(store.list_tasks()) == 2


# --------------------------------------------------------------- 状态迁移


def test_full_happy_path_records_events(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=60)
    for target in (
        TaskState.RUNNING,
        TaskState.RESULT_RECEIVED,
        TaskState.ACCEPTANCE_PENDING,
        TaskState.ACCEPTED,
    ):
        store.transition("t1", target, reason="测试推进")

    record = store.get_task("t1")
    assert record.state is TaskState.ACCEPTED
    assert record.attempts == 1
    assert record.lease_owner is None
    assert record.lease_expires_at is None
    assert [event.to_state for event in store.events("t1")] == [
        TaskState.PENDING,
        TaskState.FEASIBILITY_CHECK,
        TaskState.READY,
        TaskState.CLAIMED,
        TaskState.RUNNING,
        TaskState.RESULT_RECEIVED,
        TaskState.ACCEPTANCE_PENDING,
        TaskState.ACCEPTED,
    ]


def test_illegal_transition_rejected_and_unchanged(store):
    make_ready(store, "t1")
    events_before = store.events("t1")

    with pytest.raises(IllegalTransition):
        store.transition("t1", TaskState.ACCEPTED)

    assert store.get_task("t1").state is TaskState.READY
    assert store.events("t1") == events_before


def test_terminal_state_rejects_further_transitions(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1")
    for target in (
        TaskState.RUNNING,
        TaskState.RESULT_RECEIVED,
        TaskState.ACCEPTANCE_PENDING,
        TaskState.ACCEPTED,
    ):
        store.transition("t1", target)

    with pytest.raises(TerminalStateError):
        store.transition("t1", TaskState.RUNNING)
    with pytest.raises(ClaimRejectedError):
        store.claim("t1", "w1", idempotency_key="k2")
    assert store.get_task("t1").state is TaskState.ACCEPTED


def test_transition_to_claimed_requires_claim(store):
    make_ready(store, "t1")
    with pytest.raises(StoreError):
        store.transition("t1", TaskState.CLAIMED)
    assert store.get_task("t1").state is TaskState.READY


def test_transition_uses_table_reason_by_default(store):
    store.create_task("t1")
    store.transition("t1", TaskState.FEASIBILITY_CHECK)

    assert "可行性检查" in store.events("t1")[-1].reason


# --------------------------------------------------------------- 领取与幂等键


def test_claim_sets_lease_and_attempt(store, clock):
    make_ready(store, "t1")
    record = store.claim("t1", "w1", idempotency_key="k1", lease_seconds=60)

    assert record.state is TaskState.CLAIMED
    assert record.attempts == 1
    assert record.lease_owner == "w1"
    assert record.lease_expires_at == clock.current + timedelta(seconds=60)
    assert record.idempotency_key == "k1"
    claim_event = store.events("t1")[-1]
    assert claim_event.from_state is TaskState.READY
    assert claim_event.to_state is TaskState.CLAIMED
    assert claim_event.idempotency_key == "k1"
    assert "w1" in claim_event.reason


def test_claim_rejected_when_task_not_ready(store):
    store.create_task("t1")
    with pytest.raises(ClaimRejectedError):
        store.claim("t1", "w1", idempotency_key="k1")
    assert store.get_task("t1").state is TaskState.PENDING


def test_duplicate_idempotency_key_rejected_for_same_task(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1")
    events_before = store.events("t1")

    with pytest.raises(DuplicateClaimError) as excinfo:
        store.claim("t1", "w1", idempotency_key="k1")

    assert excinfo.value.idempotency_key == "k1"
    assert store.get_task("t1").state is TaskState.CLAIMED
    assert store.get_task("t1").attempts == 1
    assert store.events("t1") == events_before


def test_duplicate_idempotency_key_rejected_across_tasks(store):
    make_ready(store, "t1")
    make_ready(store, "t2")
    store.claim("t1", "w1", idempotency_key="k1")

    with pytest.raises(DuplicateClaimError):
        store.claim("t2", "w2", idempotency_key="k1")

    second = store.get_task("t2")
    assert second.state is TaskState.READY
    assert second.attempts == 0
    assert second.lease_owner is None


def test_duplicate_key_rejected_even_after_lease_expiry_and_recovery(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=30)
    clock.advance(31)
    store.expire_leases()
    store.transition("t1", TaskState.FEASIBILITY_CHECK)
    store.transition("t1", TaskState.READY)

    with pytest.raises(DuplicateClaimError):
        store.claim("t1", "w2", idempotency_key="k1")

    # 换一个新键可以重新领取。
    record = store.claim("t1", "w2", idempotency_key="k2")
    assert record.state is TaskState.CLAIMED
    assert record.attempts == 2


def test_idempotency_key_survives_reopen(tmp_path, clock):
    path = tmp_path / "tasks.db"
    with TaskStore(path, clock=clock) as first:
        make_ready(first, "t1")
        first.claim("t1", "w1", idempotency_key="k1")

    with TaskStore(path, clock=clock) as second:
        make_ready(second, "t2")
        with pytest.raises(DuplicateClaimError):
            second.claim("t2", "w2", idempotency_key="k1")
        assert second.get_task("t2").state is TaskState.READY


def test_idempotency_key_rejected_across_connections(tmp_path, clock):
    path = tmp_path / "tasks.db"
    with TaskStore(path, clock=clock) as first, TaskStore(path, clock=clock) as second:
        make_ready(first, "t1")
        make_ready(second, "t2")
        first.claim("t1", "w1", idempotency_key="shared")

        with pytest.raises(DuplicateClaimError):
            second.claim("t2", "w2", idempotency_key="shared")

        assert second.get_task("t2").state is TaskState.READY


def test_claim_after_retry_wait_increments_attempts(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1")
    store.transition("t1", TaskState.RUNNING)
    store.transition("t1", TaskState.RETRY_WAIT)

    record = store.claim("t1", "w2", idempotency_key="k2")
    assert record.state is TaskState.CLAIMED
    assert record.attempts == 2
    assert record.lease_owner == "w2"


def test_claim_for_returns_consumed_key(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1")

    key, worker, claimed_at = store.claim_for("t1")
    assert (key, worker) == ("k1", "w1")
    assert claimed_at == NOW
    assert store.claim_for("unknown-task") is None


def test_claim_argument_validation(store):
    make_ready(store, "t1")
    with pytest.raises(StoreError):
        store.claim("t1", "w1", idempotency_key="")
    with pytest.raises(StoreError):
        store.claim("t1", "", idempotency_key="k1")
    with pytest.raises(StoreError):
        store.claim("t1", "w1", idempotency_key="k1", lease_seconds=0)
    with pytest.raises(StoreError):
        store.claim("t1", "w1", idempotency_key="k1", now=datetime(2026, 1, 1))


# --------------------------------------------------------------- 租约


def test_expire_leases_moves_claimed_task_to_recovery_required(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=30)
    clock.advance(30)  # 到期即视为过期

    expired = store.expire_leases()

    assert [record.task_id for record in expired] == ["t1"]
    record = store.get_task("t1")
    assert record.state is TaskState.RECOVERY_REQUIRED
    assert record.lease_owner is None
    assert record.lease_expires_at is None
    event = store.events("t1")[-1]
    assert event.from_state is TaskState.CLAIMED
    assert event.to_state is TaskState.RECOVERY_REQUIRED
    assert "租约过期" in event.reason


def test_expire_leases_keeps_unexpired_lease(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=30)
    clock.advance(29)

    assert store.expire_leases() == ()
    assert store.get_task("t1").state is TaskState.CLAIMED
    assert store.get_task("t1").lease_owner == "w1"


def test_expire_leases_moves_running_task(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=10)
    store.transition("t1", TaskState.RUNNING)
    clock.advance(11)

    assert store.expire_leases()[0].state is TaskState.RECOVERY_REQUIRED
    event = store.events("t1")[-1]
    assert event.from_state is TaskState.RUNNING
    assert event.to_state is TaskState.RECOVERY_REQUIRED


def test_expire_leases_handles_multiple_tasks(store, clock):
    for task_id in ("t1", "t2"):
        make_ready(store, task_id)
        store.claim(task_id, "w1", idempotency_key=f"key-{task_id}", lease_seconds=10)
    make_ready(store, "t3")
    clock.advance(12)

    expired = store.expire_leases()

    assert [record.task_id for record in expired] == ["t1", "t2"]
    assert all(r.state is TaskState.RECOVERY_REQUIRED for r in expired)
    assert store.get_task("t3").state is TaskState.READY


def test_recovery_required_cannot_jump_to_ready(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=10)
    clock.advance(11)
    store.expire_leases()

    with pytest.raises(IllegalTransition):
        store.transition("t1", TaskState.READY)

    assert store.get_task("t1").state is TaskState.RECOVERY_REQUIRED


def test_recovery_flow_requires_recheck_then_can_claim(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=10)
    clock.advance(11)
    store.expire_leases()

    store.transition("t1", TaskState.FEASIBILITY_CHECK)
    store.transition("t1", TaskState.READY)
    record = store.claim("t1", "w2", idempotency_key="k2", lease_seconds=10)

    assert record.state is TaskState.CLAIMED
    assert record.attempts == 2
    assert [event.to_state for event in store.events("t1")][-4:] == [
        TaskState.RECOVERY_REQUIRED,
        TaskState.FEASIBILITY_CHECK,
        TaskState.READY,
        TaskState.CLAIMED,
    ]


def test_renew_lease_extends_expiry(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=30)
    clock.advance(20)
    record = store.renew_lease("t1", "w1", lease_seconds=30)

    assert record.lease_expires_at == clock.current + timedelta(seconds=30)
    clock.advance(29)
    assert store.expire_leases() == ()
    assert store.get_task("t1").state is TaskState.CLAIMED


def test_renew_lease_requires_owner(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=30)

    with pytest.raises(LeaseConflictError):
        store.renew_lease("t1", "w2", lease_seconds=30)


def test_renew_expired_lease_rejected(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=10)
    clock.advance(11)

    with pytest.raises(LeaseConflictError):
        store.renew_lease("t1", "w1", lease_seconds=10)


def test_release_claim_returns_task_to_ready(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=30)

    record = store.release_claim("t1", "w1")

    assert record.state is TaskState.READY
    assert record.lease_owner is None
    assert record.lease_expires_at is None
    assert record.attempts == 1
    assert store.events("t1")[-1].to_state is TaskState.READY


def test_release_claim_rejected_after_running(store):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1")
    store.transition("t1", TaskState.RUNNING)

    with pytest.raises(ClaimRejectedError):
        store.release_claim("t1", "w1")

    assert store.get_task("t1").state is TaskState.RUNNING


def test_lease_survives_reopen(tmp_path, clock):
    path = tmp_path / "tasks.db"
    with TaskStore(path, clock=clock) as first:
        make_ready(first, "t1")
        first.claim("t1", "w1", idempotency_key="k1", lease_seconds=60)

    with TaskStore(path, clock=clock) as second:
        record = second.get_task("t1")
        assert record.state is TaskState.CLAIMED
        assert record.lease_owner == "w1"
        assert record.lease_expires_at == NOW + timedelta(seconds=60)
        assert second.renew_lease("t1", "w1", lease_seconds=60).lease_owner == "w1"


# --------------------------------------------------------------- 事件一致性


def test_events_chain_matches_recorded_states(store, clock):
    make_ready(store, "t1")
    store.claim("t1", "w1", idempotency_key="k1", lease_seconds=5)
    store.transition("t1", TaskState.RUNNING)
    clock.advance(6)
    store.expire_leases()

    events = store.events("t1")
    assert [event.sequence for event in events] == list(
        range(events[0].sequence, events[0].sequence + len(events))
    )
    for previous, current in zip(events, events[1:]):
        assert current.from_state is previous.to_state
    assert events[-1].to_state is store.get_task("t1").state
