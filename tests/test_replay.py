"""从 JSONL 重放任务状态，并验证非法序列会报错（规格 4.3、7）。"""

import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from feasibility_scheduler.events import EventLog, hash_config
from feasibility_scheduler.replay import ReplayError, replay, replay_file
from feasibility_scheduler.statemachine import TaskState

FIXED = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
P = TaskState.PENDING
F = TaskState.FEASIBILITY_CHECK
R = TaskState.READY
C = TaskState.CLAIMED
RUN = TaskState.RUNNING
RR = TaskState.RESULT_RECEIVED
AP = TaskState.ACCEPTANCE_PENDING
A = TaskState.ACCEPTED


def fixed_clock():
    ticks = itertools.count()
    return lambda: FIXED + timedelta(seconds=next(ticks))


def make_log(tmp_path, run_id="run-1"):
    return EventLog(
        tmp_path / "events.jsonl",
        run_id=run_id,
        source_revision="rev-1",
        config_hash=hash_config({"max_concurrency": 2}),
        clock=fixed_clock(),
    )


def record(log, event_type, task_id, previous, new, reason="", **kwargs):
    return log.record(
        event_type,
        component="runner",
        task_id=task_id,
        previous_state=previous,
        new_state=new,
        decision_reason=reason,
        **kwargs,
    )


def build_valid_run(log):
    log.record("run_created", component="runner", decision_reason="创建运行")
    record(log, "task_validated", "task-1", None, P, "任务登记")
    record(log, "phi_evaluated", "task-1", P, F, "开始可行性检查")
    record(log, "task_ready", "task-1", F, R, "门禁通过")
    record(log, "task_claimed", "task-1", R, C, "Worker 领取任务")
    record(log, "api_request_started", "task-1", C, RUN, "开始调用模型")
    record(log, "api_response_received", "task-1", RUN, RR, "收到响应")
    record(log, "acceptance_started", "task-1", RR, AP, "进入独立验收")
    record(log, "acceptance_passed", "task-1", AP, A, "验收通过")
    log.record("task_completed", component="runner", task_id="task-1")
    log.record("run_completed", component="runner")


def test_valid_log_replays_to_accepted(tmp_path):
    log = make_log(tmp_path)
    build_valid_run(log)

    result = replay_file(log.path)

    assert result.run_id == "run-1"
    assert result.state_of("task-1") is A
    assert result.event_count == 11
    assert result.all_terminal is True
    task = result.tasks["task-1"]
    assert task.transitions[0] == (None, P)
    assert task.transitions[-1] == (AP, A)
    assert len(task.event_ids) == 8


def test_directly_jumping_to_running_is_rejected(tmp_path):
    log = make_log(tmp_path)
    record(log, "task_validated", "task-1", None, P, "任务登记")
    illegal = record(log, "task_ready", "task-1", P, RUN, "跳过就绪直接执行")

    with pytest.raises(ReplayError) as info:
        replay_file(log.path)

    message = str(info.value)
    assert illegal.event_id in message
    assert "PENDING" in message
    assert "RUNNING" in message
    assert "迁移表" in message


def test_terminal_state_cannot_transition_again(tmp_path):
    log = make_log(tmp_path)
    build_valid_run(log)
    record(log, "task_ready", "task-1", A, R, "终态之后又派发")

    with pytest.raises(ReplayError) as info:
        replay_file(log.path)
    assert "ACCEPTED" in str(info.value)


def test_previous_state_must_match_replayed_state(tmp_path):
    log = make_log(tmp_path)
    record(log, "task_validated", "task-1", None, P, "任务登记")
    record(log, "phi_evaluated", "task-1", P, F, "开始可行性检查")
    record(log, "task_claimed", "task-1", R, C, "声称从 READY 领取")

    with pytest.raises(ReplayError) as info:
        replay_file(log.path)
    assert "不一致" in str(info.value)
    assert "FEASIBILITY_CHECK" in str(info.value)


def test_first_state_must_be_pending(tmp_path):
    log = make_log(tmp_path)
    record(log, "task_ready", "task-1", None, R, "首次出现就是 READY")

    with pytest.raises(ReplayError) as info:
        replay_file(log.path)
    assert "首次出现必须是 PENDING" in str(info.value)


def test_missing_previous_state_after_task_exists_is_rejected(tmp_path):
    log = make_log(tmp_path)
    record(log, "task_validated", "task-1", None, P, "任务登记")
    record(log, "task_ready", "task-1", None, R, "缺少 previous_state")

    with pytest.raises(ReplayError) as info:
        replay_file(log.path)
    assert "缺少 previous_state" in str(info.value)


def test_previous_state_without_new_state_is_rejected(tmp_path):
    log = make_log(tmp_path)
    log.record(
        "task_ready",
        component="gate",
        task_id="task-1",
        previous_state=R,
    )

    with pytest.raises(ReplayError) as info:
        replay_file(log.path)
    assert "没有 new_state" in str(info.value)


def test_empty_log_is_rejected():
    with pytest.raises(ReplayError):
        replay(())


def test_corrupt_jsonl_is_reported_by_replay_file(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(ReplayError) as info:
        replay_file(path)
    assert "无法读取" in str(info.value)


def test_events_from_two_runs_cannot_be_mixed(tmp_path):
    left = make_log(tmp_path / "left")
    right = make_log(tmp_path / "right", run_id="run-2")
    left.record("run_created", component="runner")
    right.record("run_created", component="runner")

    with pytest.raises(ReplayError) as info:
        replay(left.events() + right.events())
    assert "run-2" in str(info.value)


def test_sequence_must_strictly_increase(tmp_path):
    log = make_log(tmp_path)
    first = log.record("run_created", component="runner")
    second = log.record("run_created", component="runner")
    first_data = first.to_dict()
    second_data = second.to_dict()
    second_data["sequence"] = first.sequence
    log.path.write_text(
        json.dumps(first_data, ensure_ascii=False)
        + "\n"
        + json.dumps(second_data, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReplayError) as info:
        replay_file(log.path)
    assert "sequence" in str(info.value)


def test_state_of_unknown_task_reports_error(tmp_path):
    log = make_log(tmp_path)
    build_valid_run(log)
    result = replay_file(log.path)

    with pytest.raises(ReplayError):
        result.state_of("missing")
