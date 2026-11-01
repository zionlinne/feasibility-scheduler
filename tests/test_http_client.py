"""HttpClient 单元测试：用假 opener，不触网、不读真实 key。"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from email.message import Message

import pytest

from feasibility_scheduler.fake_client import (
    ApiRequest,
    Outcome,
    RequestStatus,
)
from feasibility_scheduler.http_client import HttpClient, MissingApiKeyError


def make_request(key="idem-1", task="task-1", prompt='{"ok": true}', model="gpt-6-astra"):
    return ApiRequest(task_id=task, idempotency_key=key, prompt=prompt, model=model)


class FakeHTTPResponse:
    def __init__(self, body: str, status: int = 200, headers: dict | None = None):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_opener(body_obj=None, *, body_text=None, status=200, headers=None, error=None):
    payload = body_text if body_text is not None else json.dumps(body_obj)

    def opener(req, timeout=None):
        if error is not None:
            raise error
        return FakeHTTPResponse(payload, status=status, headers=headers)

    return opener


def openai_success(content='{"ok": true}', **usage):
    return {
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 3),
            "completion_tokens": usage.get("completion_tokens", 2),
        },
    }


def test_success_parses_assistant_json(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    client = HttpClient(opener=make_opener(openai_success()))
    response = client.call(make_request())

    assert response.ok
    assert response.outcome is Outcome.SUCCESS
    assert dict(response.parsed_body) == {"ok": True}
    assert response.request_id == "chatcmpl-test"
    assert dict(response.usage) == {"input_tokens": 3, "output_tokens": 2}
    assert client.request_status("idem-1") is RequestStatus.SENT
    assert "sk-unit-test-key" not in (response.raw_body or "")


def test_rate_limited(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    client = HttpClient(
        opener=make_opener(
            {"error": "rate"},
            status=429,
            headers={"Retry-After": "1.5"},
        )
    )
    response = client.call(make_request())
    assert response.outcome is Outcome.RATE_LIMITED
    assert response.retryable
    assert response.retry_after_seconds == 1.5


def test_server_error(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    client = HttpClient(opener=make_opener({"error": "boom"}, status=503))
    response = client.call(make_request())
    assert response.outcome is Outcome.SERVER_ERROR
    assert response.retryable


def test_timeout_maps_to_unknown(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")

    def opener(req, timeout=None):
        raise TimeoutError("timed out")

    client = HttpClient(opener=opener)
    response = client.call(make_request())
    assert response.outcome is Outcome.TIMEOUT
    assert client.request_status("idem-1") is RequestStatus.UNKNOWN


def test_non_json_assistant_content_is_invalid(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    client = HttpClient(opener=make_opener(openai_success(content="not-json")))
    response = client.call(make_request())
    assert response.outcome is Outcome.INVALID_JSON
    assert response.error_type == "invalid_format_no_artifact"
    assert response.retryable is False
    assert response.format_repair_used is False
    assert client.repair_prompts == []


def test_duplicate_does_not_resend(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    calls = {"n": 0}

    def opener(req, timeout=None):
        calls["n"] += 1
        return FakeHTTPResponse(json.dumps(openai_success()))

    client = HttpClient(opener=opener)
    first = client.call(make_request())
    second = client.call(make_request())
    assert calls["n"] == 1
    assert second.is_duplicate is True
    assert second.duplicate_of == first.request_id


def test_missing_key_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("FEASIBILITY_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FEASIBILITY_API_KEY_FILE", raising=False)
    missing = tmp_path / "no-such.dpapi"
    with pytest.raises(MissingApiKeyError):
        HttpClient(key_file=missing, opener=make_opener(openai_success()))


def test_acall(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    client = HttpClient(opener=make_opener(openai_success()))
    import asyncio

    response = asyncio.run(client.acall(make_request()))
    assert response.ok


def openai_truncated(content='{"ok": true, "files": {"a.mjs": "export const', **usage):
    """模拟触顶截断：finish_reason=length 且 completion_tokens=65536。"""
    return {
        "id": "chatcmpl-trunc",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "length",
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 10),
            "completion_tokens": usage.get("completion_tokens", 65536),
        },
    }


def test_output_truncated_marked_when_finish_reason_length(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    client = HttpClient(opener=make_opener(openai_truncated()))
    response = client.call(make_request())
    assert response.outcome is Outcome.INVALID_JSON
    assert response.error_type == "output_truncated"
    assert response.retryable is False
    assert dict(response.usage).get("output_tokens") == 65536


def test_output_truncated_marked_when_tokens_hit_hard_limit(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    # 无 finish_reason，但 completion_tokens 触顶且 JSON 残缺
    payload = {
        "id": "chatcmpl-trunc2",
        "choices": [
            {"message": {"role": "assistant", "content": '{"ok": true, "files": {'}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 65536},
    }
    client = HttpClient(opener=make_opener(payload))
    response = client.call(make_request())
    assert response.outcome is Outcome.INVALID_JSON
    assert response.error_type == "output_truncated"


def test_plain_invalid_json_not_marked_truncated(monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    payload = openai_success(content="not-json-at-all", completion_tokens=12)
    # openai_success puts kwargs into usage; ensure finish_reason stop
    payload["choices"][0]["finish_reason"] = "stop"
    client = HttpClient(opener=make_opener(payload))
    response = client.call(make_request())
    assert response.outcome is Outcome.INVALID_JSON
    assert response.error_type == "invalid_format_no_artifact"
    assert response.format_repair_used is False
