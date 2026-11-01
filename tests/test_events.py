"""JSONL 事件日志的字段、追加、密钥与坏行处理测试（规格第 7 节）。"""

import itertools
import json
from datetime import datetime, timedelta, timezone

import pytest

from feasibility_scheduler.events import (
    EVENT_TYPES,
    REQUIRED_FIELDS,
    Event,
    EventLog,
    EventLogError,
    EventLogFormatError,
    InvalidEventError,
    SecretLeakError,
    hash_config,
    read_events,
)
from feasibility_scheduler.fake_client import ApiRequest, FakeClient, Step
from feasibility_scheduler.statemachine import TaskState

FIXED = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


def fixed_clock(start=FIXED):
    ticks = itertools.count()
    return lambda: start + timedelta(seconds=next(ticks))


def make_log(tmp_path, run_id="run-1", name="events.jsonl"):
    return EventLog(
        tmp_path / name,
        run_id=run_id,
        source_revision="rev-1",
        config_hash=hash_config({"max_concurrency": 2}),
        clock=fixed_clock(),
    )


def test_every_event_has_all_required_fields(tmp_path):
    log = make_log(tmp_path)
    event = log.record("run_created", component="runner", decision_reason="创建运行")

    data = event.to_dict()
    assert set(data) == set(REQUIRED_FIELDS)
    assert data["run_id"] == "run-1"
    assert data["event_id"]
    assert data["sequence"] == 0
    assert data["task_id"] is None
    assert data["candidate_id"] is None
    assert data["previous_state"] is None
    assert data["new_state"] is None
    assert data["evidence_refs"] == []
    assert data["details"] == {}
    assert data["config_hash"].startswith("sha256:")
    assert data["source_revision"] == "rev-1"

    line = log.path.read_text(encoding="utf-8").strip()
    assert json.loads(line) == data


def test_accepts_every_spec_event_type_and_numbers_events(tmp_path):
    log = make_log(tmp_path)
    for event_type in sorted(EVENT_TYPES):
        log.record(event_type, component="test")

    events = log.events()
    assert len(events) == len(EVENT_TYPES)
    assert [event.type for event in events] == sorted(EVENT_TYPES)
    assert [event.sequence for event in events] == list(range(len(EVENT_TYPES)))
    assert len({event.event_id for event in events}) == len(EVENT_TYPES)
    assert len(log.path.read_text(encoding="utf-8").splitlines()) == len(EVENT_TYPES)


def test_append_only_log_resumes_after_reopen(tmp_path):
    log = make_log(tmp_path)
    log.record("run_created", component="runner")
    log.record("task_validated", component="validation")

    reopened = make_log(tmp_path)
    third = reopened.record("run_completed", component="runner")

    assert third.sequence == 2
    assert [event.sequence for event in reopened.events()] == [0, 1, 2]
    assert len(log.path.read_text(encoding="utf-8").splitlines()) == 3


def test_reopen_with_other_run_id_is_rejected(tmp_path):
    log = make_log(tmp_path)
    log.record("run_created", component="runner")

    with pytest.raises(EventLogError):
        make_log(tmp_path, run_id="run-2")


def test_api_key_in_details_is_rejected_and_nothing_is_written(tmp_path):
    log = make_log(tmp_path)

    with pytest.raises(SecretLeakError):
        log.record(
            "api_request_started",
            component="executor",
            details={"api_key": "sk-live-1234567890"},
        )

    assert len(log) == 0
    assert log.path.read_text(encoding="utf-8") == ""


def test_authorization_header_is_rejected(tmp_path):
    log = make_log(tmp_path)

    with pytest.raises(SecretLeakError):
        log.record(
            "api_request_started",
            component="executor",
            details={"Authorization": "Bearer abcdefghijklmnop"},
        )

    assert len(log) == 0


def test_secret_in_free_text_is_rejected(tmp_path):
    log = make_log(tmp_path)

    with pytest.raises(SecretLeakError):
        log.record(
            "api_request_failed",
            component="executor",
            decision_reason="请求时使用了 sk-test-secret-key-123456",
        )

    assert len(log) == 0


def test_usage_tokens_are_not_mistaken_for_secrets(tmp_path):
    log = make_log(tmp_path)
    log.record(
        "api_response_received",
        component="executor",
        details={"usage": {"input_tokens": 12, "output_tokens": 7}},
    )

    assert len(log) == 1
    assert "input_tokens" in log.path.read_text(encoding="utf-8")


def test_unknown_event_type_is_rejected(tmp_path):
    log = make_log(tmp_path)

    with pytest.raises(InvalidEventError):
        log.record("not_a_spec_event", component="runner")

    assert len(log) == 0


def test_naive_timestamp_is_rejected(tmp_path):
    log = EventLog(
        tmp_path / "naive.jsonl",
        run_id="run-naive",
        clock=lambda: datetime(2026, 1, 1),
    )

    with pytest.raises(InvalidEventError):
        log.record("run_created", component="runner")


def test_event_requires_task_id_when_state_is_present(tmp_path):
    log = make_log(tmp_path)
    event = log.record(
        "task_ready",
        component="gate",
        task_id="task-1",
        previous_state=TaskState.FEASIBILITY_CHECK,
        new_state=TaskState.READY,
    )

    assert event.previous_state is TaskState.FEASIBILITY_CHECK
    assert event.new_state is TaskState.READY


def test_from_dict_rejects_missing_fields(tmp_path):
    event = make_log(tmp_path).record("run_created", component="runner")
    data = event.to_dict()
    del data["config_hash"]

    with pytest.raises(InvalidEventError):
        Event.from_dict(data)


def test_bad_line_is_reported_not_skipped(tmp_path):
    log = make_log(tmp_path)
    first = log.record("run_created", component="runner")
    path = log.path
    good = json.dumps(first.to_dict(), ensure_ascii=False)
    path.write_text(good + "\nnot json\n" + good + "\n", encoding="utf-8")

    with pytest.raises(EventLogFormatError) as info:
        read_events(path)
    assert "第 2 行" in str(info.value)


def test_blank_line_is_reported(tmp_path):
    log = make_log(tmp_path)
    log.record("run_created", component="runner")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(EventLogFormatError) as info:
        read_events(log.path)
    assert "空行" in str(info.value)


def test_duplicate_event_id_is_reported(tmp_path):
    log = make_log(tmp_path)
    event = log.record("run_created", component="runner")
    line = json.dumps(event.to_dict(), ensure_ascii=False)
    log.path.write_text(line + "\n" + line + "\n", encoding="utf-8")

    with pytest.raises(EventLogFormatError) as info:
        read_events(log.path)
    assert "重复" in str(info.value)


def test_config_hash_is_order_independent():
    left = hash_config({"b": 1, "a": {"y": 2, "x": 3}})
    right = hash_config({"a": {"x": 3, "y": 2}, "b": 1})

    assert left == right
    assert left.startswith("sha256:")
    assert left != hash_config({"b": 2, "a": {"y": 2, "x": 3}})


def test_api_response_audit_fields_enter_log_without_prompt_or_key(tmp_path):
    log = make_log(tmp_path)
    client = FakeClient([Step.success('{"answer": 42}')])
    request = ApiRequest(
        task_id="task-1",
        idempotency_key="idem-1",
        prompt="不要写进日志的提示词",
    )
    response = client.call(request)

    log.record(
        "api_response_received",
        component="executor",
        task_id="task-1",
        previous_state=TaskState.RUNNING,
        new_state=TaskState.RESULT_RECEIVED,
        decision_reason="收到 provider 响应",
        evidence_refs=(response.request_id,),
        task_input_hash=response.input_artifact_hash,
        details=response.to_audit_dict(),
    )

    text = log.path.read_text(encoding="utf-8")
    stored = json.loads(text)["details"]
    assert stored["provider"] == "fake"
    assert stored["request_id"] == response.request_id
    assert stored["usage"] == {"input_tokens": 8, "output_tokens": 5}
    assert "不要写进日志的提示词" not in text
    assert "sk-live" not in text
    assert "sk-test" not in text
    assert stored["input_artifact_hash"].startswith("sha256:")
