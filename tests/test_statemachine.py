"""statemachine.py 测试：迁移表完整性、终态封闭、租约过期唯一出口。

测试预期来自规格第 4.3 节与任务目标：终态为 ACCEPTED / FAILED / CANCELLED。
"""

from itertools import product
from types import MappingProxyType

import pytest

from feasibility_scheduler.statemachine import (
    ALLOWED_TRANSITIONS,
    LEASE_EXPIRY_TARGET,
    LEASE_HOLDING_STATES,
    TERMINAL_STATES,
    TRANSITION_REASONS,
    IllegalTransition,
    LeaseNotHeldError,
    StateMachineError,
    TaskState,
    TerminalStateError,
    allowed_targets,
    can_transition,
    check_transition,
    is_terminal,
    iter_transitions,
    parse_state,
    recoverable_states,
    transition,
    transition_on_lease_expiry,
)

ALL_STATES = list(TaskState)
LEGAL_EDGES = {(item.source, item.target) for item in iter_transitions()}
NON_TERMINAL = [state for state in ALL_STATES if state not in TERMINAL_STATES]


# --------------------------------------------------------------- 迁移表本体


def test_terminal_states_are_exactly_accepted_failed_cancelled():
    assert TERMINAL_STATES == {
        TaskState.ACCEPTED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    }
    assert "COMPLETED" not in TaskState.__members__


def test_table_has_no_duplicate_and_no_self_loop_and_no_terminal_source():
    edges = [(item.source, item.target) for item in iter_transitions()]
    assert len(edges) == len(set(edges)), "迁移表不应有重复边"
    assert all(source is not target for source, target in edges), "不应存在自迁移"
    assert all(source not in TERMINAL_STATES for source, _ in edges)


def test_every_edge_has_reason():
    for edge, reason in TRANSITION_REASONS.items():
        assert reason.strip(), f"{edge} 缺少说明"


def test_all_states_reachable_from_pending():
    seen = {TaskState.PENDING}
    frontier = [TaskState.PENDING]
    while frontier:
        state = frontier.pop()
        for target in allowed_targets(state):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    assert seen == set(TaskState)


def test_transition_mapping_is_read_only():
    assert isinstance(ALLOWED_TRANSITIONS, MappingProxyType)
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[TaskState.PENDING] = frozenset()  # type: ignore[index]
    assert isinstance(ALLOWED_TRANSITIONS[TaskState.PENDING], frozenset)


@pytest.mark.parametrize("source,target", list(product(ALL_STATES, ALL_STATES)))
def test_only_table_edges_are_allowed(source, target):
    """穷举 15x15 个状态对：表中存在即放行，否则必须拒绝。"""
    if (source, target) in LEGAL_EDGES:
        assert can_transition(source, target)
        assert transition(source, target) is target
        assert check_transition(source, target).reason.strip()
    else:
        assert not can_transition(source, target)
        with pytest.raises(IllegalTransition) as excinfo:
            transition(source, target)
        assert excinfo.value.source is source
        assert excinfo.value.target is target


@pytest.mark.parametrize("source", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_states_reject_every_target(source):
    assert allowed_targets(source) == frozenset()
    for target in ALL_STATES:
        with pytest.raises(TerminalStateError) as excinfo:
            transition(source, target)
        assert excinfo.value.from_terminal
        assert "终态" in str(excinfo.value)


def test_terminal_error_is_illegal_transition_and_value_error():
    with pytest.raises(IllegalTransition):
        transition(TaskState.ACCEPTED, TaskState.RUNNING)
    with pytest.raises(ValueError):
        transition(TaskState.FAILED, TaskState.READY)


def test_illegal_transition_message_lists_allowed_targets():
    with pytest.raises(IllegalTransition) as excinfo:
        transition(TaskState.PENDING, TaskState.RUNNING)
    message = str(excinfo.value)
    assert "PENDING -> RUNNING" in message
    assert "FEASIBILITY_CHECK" in message
    assert "CANCELLED" in message


def test_string_states_are_accepted():
    assert parse_state("READY") is TaskState.READY
    assert transition("PENDING", "FEASIBILITY_CHECK") is TaskState.FEASIBILITY_CHECK
    assert is_terminal("CANCELLED")
    assert not is_terminal("READY")


def test_unknown_state_is_rejected():
    with pytest.raises(StateMachineError):
        parse_state("NOT_A_STATE")
    with pytest.raises(StateMachineError):
        transition(TaskState.PENDING, "NOT_A_STATE")


# --------------------------------------------------------------- 主流程


def test_happy_path_reaches_accepted():
    path = [
        TaskState.PENDING,
        TaskState.FEASIBILITY_CHECK,
        TaskState.READY,
        TaskState.CLAIMED,
        TaskState.RUNNING,
        TaskState.RESULT_RECEIVED,
        TaskState.ACCEPTANCE_PENDING,
        TaskState.ACCEPTED,
    ]
    for source, target in zip(path, path[1:]):
        assert transition(source, target) is target
    assert is_terminal(path[-1])


def test_retry_loop_is_legal():
    assert transition(TaskState.RUNNING, TaskState.RETRY_WAIT) is TaskState.RETRY_WAIT
    assert transition(TaskState.RETRY_WAIT, TaskState.CLAIMED) is TaskState.CLAIMED
    assert transition(TaskState.CLAIMED, TaskState.RUNNING) is TaskState.RUNNING


def test_acceptance_rejection_paths():
    assert transition(
        TaskState.RESULT_RECEIVED, TaskState.REJECTED
    ) is TaskState.REJECTED
    assert transition(
        TaskState.ACCEPTANCE_PENDING, TaskState.REJECTED
    ) is TaskState.REJECTED
    assert transition(TaskState.REJECTED, TaskState.RETRY_WAIT) is TaskState.RETRY_WAIT
    assert transition(TaskState.REJECTED, TaskState.FAILED) is TaskState.FAILED


@pytest.mark.parametrize("state", NON_TERMINAL)
def test_every_non_terminal_state_can_be_cancelled(state):
    assert transition(state, TaskState.CANCELLED) is TaskState.CANCELLED


# --------------------------------------------------------------- 租约过期


@pytest.mark.parametrize("state", ALL_STATES)
def test_lease_expiry_only_reaches_recovery_required(state):
    if state in LEASE_HOLDING_STATES:
        assert transition_on_lease_expiry(state) is TaskState.RECOVERY_REQUIRED
    else:
        with pytest.raises(LeaseNotHeldError):
            transition_on_lease_expiry(state)


def test_lease_expiry_target_is_never_ready():
    assert LEASE_EXPIRY_TARGET is TaskState.RECOVERY_REQUIRED
    assert LEASE_EXPIRY_TARGET is not TaskState.READY
    for state in LEASE_HOLDING_STATES:
        assert transition_on_lease_expiry(state) is not TaskState.READY


def test_lease_holding_states_are_claimed_and_running():
    assert LEASE_HOLDING_STATES == {TaskState.CLAIMED, TaskState.RUNNING}


def test_recovery_required_has_no_direct_ready_transition():
    assert not can_transition(TaskState.RECOVERY_REQUIRED, TaskState.READY)
    assert TaskState.READY not in allowed_targets(TaskState.RECOVERY_REQUIRED)
    assert (TaskState.RECOVERY_REQUIRED, TaskState.READY) not in LEGAL_EDGES
    with pytest.raises(IllegalTransition):
        transition(TaskState.RECOVERY_REQUIRED, TaskState.READY)


def test_recovery_required_cannot_claim_directly():
    """恢复后不能直接回到 CLAIMED，必须先重新过门禁。"""
    assert not can_transition(TaskState.RECOVERY_REQUIRED, TaskState.CLAIMED)


def test_recovery_required_must_recheck_before_ready():
    assert transition(
        TaskState.RECOVERY_REQUIRED, TaskState.FEASIBILITY_CHECK
    ) is TaskState.FEASIBILITY_CHECK
    assert transition(
        TaskState.FEASIBILITY_CHECK, TaskState.READY
    ) is TaskState.READY


def test_recovery_required_exits_are_limited():
    assert allowed_targets(TaskState.RECOVERY_REQUIRED) == {
        TaskState.FEASIBILITY_CHECK,
        TaskState.FAILED,
        TaskState.CANCELLED,
    }


def test_recoverable_states_match_table():
    assert recoverable_states() == {
        TaskState.CLAIMED,
        TaskState.RUNNING,
        TaskState.RESULT_RECEIVED,
        TaskState.ACCEPTANCE_PENDING,
        TaskState.RETRY_WAIT,
        TaskState.REJECTED,
    }
