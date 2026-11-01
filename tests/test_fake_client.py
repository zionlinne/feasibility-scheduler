"""FakeClient 的确定性、故障模拟与幂等状态测试（规格 4.1、6.1）。"""

import asyncio
import socket
import time

import pytest

from feasibility_scheduler.fake_client import (
    DEFAULT_MODEL,
    ApiRequest,
    DuplicateRequestError,
    FakeClient,
    FakeScriptError,
    FakeScriptExhausted,
    Outcome,
    RequestStatus,
    RequestValidationError,
    Step,
)


def make_request(key="idem-1", task="task-1", prompt="总结这段文本"):
    return ApiRequest(task_id=task, idempotency_key=key, prompt=prompt)


def test_success_is_fully_offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("FakeClient 不得访问网络")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)

    client = FakeClient([Step.success('{"answer": 42}')])
    response = client.call(make_request())

    assert response.outcome is Outcome.SUCCESS
    assert response.ok is True
    assert response.status_code == 200
    assert dict(response.parsed_body) == {"answer": 42}
    assert response.request_id == "fake-000001"
    assert response.attempt == 1
    assert response.is_duplicate is False
    assert client.request_status("idem-1") is RequestStatus.SENT


def test_timeout_reports_unknown_and_does_not_wait():
    client = FakeClient([Step.timeout()])
    response = client.call(make_request())

    assert response.outcome is Outcome.TIMEOUT
    assert response.ok is False
    assert response.retryable is True
    assert response.status_code == 0
    assert response.parsed_body is None
    assert response.error_type == "timeout"
    assert client.request_status("idem-1") is RequestStatus.UNKNOWN
    assert client.attempts("idem-1") == 1
    # 逻辑时钟只推进一分钟，真实运行没有等待 30 秒。
    assert (response.finished_at - response.started_at).total_seconds() == 30.0


def test_rate_limited_carries_retry_after():
    client = FakeClient([Step.rate_limited(retry_after_seconds=3.5)])
    response = client.call(make_request())

    assert response.outcome is Outcome.RATE_LIMITED
    assert response.status_code == 429
    assert response.retryable is True
    assert response.retry_after_seconds == 3.5
    assert client.request_status("idem-1") is RequestStatus.SENT


def test_server_error_is_retryable_and_status_is_sent():
    client = FakeClient([Step.server_error(status_code=503)])
    response = client.call(make_request())

    assert response.outcome is Outcome.SERVER_ERROR
    assert response.status_code == 503
    assert response.error_type == "server_error"
    assert response.retryable is True
    assert client.request_status("idem-1") is RequestStatus.SENT


def test_invalid_json_is_a_parse_failure_not_an_exception():
    client = FakeClient([Step.invalid_json('{"broken": ')])
    response = client.call(make_request())

    assert response.outcome is Outcome.INVALID_JSON
    assert response.parsed_body is None
    assert response.raw_body == '{"broken": '
    assert response.error_type == "invalid_format_no_artifact"
    assert response.retryable is False
    assert response.format_repair_used is False
    assert client.repair_prompts == []
    assert client.request_status("idem-1") is RequestStatus.SENT


def test_success_body_that_is_not_a_json_object_is_invalid():
    client = FakeClient([Step.success("[1, 2, 3]")])
    response = client.call(make_request())

    assert response.outcome is Outcome.INVALID_JSON
    assert response.parsed_body is None
    assert response.error_type == "invalid_format_no_artifact"


def test_duplicate_response_reuses_request_id_without_new_attempt():
    client = FakeClient([Step.success('{"answer": 42}'), Step.duplicate()])
    request = make_request()

    first = client.call(request)
    second = client.call(request)

    assert second.is_duplicate is True
    assert second.duplicate_of == first.request_id
    assert second.request_id == first.request_id
    assert second.attempt == 1
    assert dict(second.parsed_body) == dict(first.parsed_body)
    assert client.attempts("idem-1") == 1
    assert client.duplicate_count("idem-1") == 1
    assert client.request_status("idem-1") is RequestStatus.SENT


def test_duplicate_before_any_success_is_a_script_error():
    client = FakeClient([Step.duplicate()])

    with pytest.raises(FakeScriptError):
        client.call(make_request())

    assert client.request_status("idem-1") is RequestStatus.NOT_SENT
    assert client.attempts("idem-1") == 0
    assert client.script_remaining == 1


def test_reusing_idempotency_key_without_duplicate_step_is_rejected():
    client = FakeClient([Step.success(), Step.success()])
    request = make_request()

    client.call(request)
    with pytest.raises(DuplicateRequestError):
        client.call(request)

    assert client.attempts("idem-1") == 1
    assert client.script_remaining == 1


def test_reusing_idempotency_key_for_another_request_is_rejected():
    client = FakeClient([Step.success(), Step.success()])
    client.call(make_request(prompt="第一段输入"))

    with pytest.raises(DuplicateRequestError):
        client.call(make_request(prompt="另一段输入"))

    assert client.attempts("idem-1") == 1
    assert client.script_remaining == 1


def test_timeout_can_be_retried_only_when_enabled():
    strict = FakeClient([Step.timeout(), Step.success()])
    strict.call(make_request())
    with pytest.raises(DuplicateRequestError):
        strict.call(make_request())

    retrying = FakeClient(
        [Step.timeout(), Step.success()], allow_retry_after_unknown=True
    )
    first = retrying.call(make_request())
    second = retrying.call(make_request())

    assert first.outcome is Outcome.TIMEOUT
    assert second.outcome is Outcome.SUCCESS
    assert second.request_id == "fake-000002"
    assert second.attempt == 2
    assert retrying.attempts("idem-1") == 2
    assert retrying.request_status("idem-1") is RequestStatus.SENT


def test_script_exhaustion_is_loud():
    client = FakeClient([])

    with pytest.raises(FakeScriptExhausted):
        client.call(make_request())

    assert client.request_status("idem-1") is RequestStatus.NOT_SENT
    assert client.attempts("idem-1") == 0


def test_same_script_and_requests_produce_identical_results():
    script = [
        Step.success('{"answer": 1}'),
        Step.rate_limited(),
        Step.timeout(),
        Step.server_error(),
        Step.invalid_json(),
    ]
    requests = [make_request(key=f"idem-{index}") for index in range(5)]

    first = FakeClient(script)
    first_audit = [first.call(request).to_audit_dict() for request in requests]

    second = FakeClient(script)
    second_audit = [second.call(request).to_audit_dict() for request in requests]
    assert first_audit == second_audit

    second.reset()
    assert second.request_status("idem-0") is RequestStatus.NOT_SENT
    assert second.attempts("idem-0") == 0
    rerun_audit = [second.call(request).to_audit_dict() for request in requests]
    assert rerun_audit == first_audit


def test_audit_fields_exclude_prompt_and_cover_spec_4_1():
    client = FakeClient([Step.success()])
    request = make_request(prompt="不要把这个提示词写进日志")
    response = client.call(request)

    audit = response.to_audit_dict()
    assert "prompt" not in audit
    assert "不要把这个提示词写进日志" not in str(audit)
    assert {
        "request_id",
        "provider",
        "model",
        "task_id",
        "input_artifact_hash",
        "started_at",
        "finished_at",
        "status_code",
        "outcome",
        "usage",
        "parse_status",
        "attempt",
    } <= set(audit)
    assert audit["provider"] == "fake"
    assert audit["model"] == DEFAULT_MODEL
    assert audit["parse_status"] == "parsed"


def test_input_hash_is_deterministic_and_sensitive_to_prompt():
    left = ApiRequest(task_id="t", idempotency_key="a", prompt="同一段输入")
    right = ApiRequest(task_id="t", idempotency_key="b", prompt="同一段输入")
    different = ApiRequest(task_id="t", idempotency_key="c", prompt="另一段输入")

    assert left.input_artifact_hash == right.input_artifact_hash
    assert left.input_artifact_hash != different.input_artifact_hash
    assert left.input_artifact_hash.startswith("sha256:")


def test_request_validation_rejects_empty_keys():
    with pytest.raises(RequestValidationError):
        ApiRequest(task_id="t", idempotency_key="", prompt="x")
    with pytest.raises(RequestValidationError):
        ApiRequest(task_id="", idempotency_key="k", prompt="x")


def test_acall_matches_sync_call():
    client = FakeClient([Step.success('{"answer": 7}')])
    response = asyncio.run(client.acall(make_request()))

    assert response.outcome is Outcome.SUCCESS
    assert dict(response.parsed_body) == {"answer": 7}
    assert client.request_status("idem-1") is RequestStatus.SENT
