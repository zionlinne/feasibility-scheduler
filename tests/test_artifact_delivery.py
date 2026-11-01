"""Artifact delivery: Raw-before-parse, ACS + JSON, one format repair."""

from __future__ import annotations

import json
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

import pytest

from feasibility_scheduler.adapters.legacy_doc import (
    _ArtifactSink,
    legacy_artifact_acceptor,
)
from feasibility_scheduler.artifact_delivery import (
    ERROR_INVALID_FORMAT_NO_ARTIFACT,
    ERROR_INVALID_JSON,
    ERROR_OUTPUT_TRUNCATED,
    ERROR_PARTIAL_ARTIFACT,
    allows_format_repair,
    merge_files,
    recovery_for,
)
from feasibility_scheduler.fake_client import ApiRequest, ApiResponse, FakeClient, Outcome, Step
from feasibility_scheduler.http_client import HttpClient
from feasibility_scheduler.models import Task


OWNED = ["src/a.mjs", "src/b.mjs"]


@dataclass
class Turn:
    content: str = ""
    raw_http: str | None = None
    finish_reason: str | None = None
    output_tokens: int | None = None


class _HTTPResponse:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = Message()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Script:
    def __init__(self, turns: list[Turn]):
        self.turns = turns
        self.calls = 0

    def opener(self, req, timeout=None):
        idx = self.calls
        self.calls += 1
        turn = self.turns[idx]
        if turn.raw_http is not None:
            return _HTTPResponse(turn.raw_http)
        payload = {
            "id": f"chatcmpl-{idx}",
            "choices": [
                {
                    "message": {"role": "assistant", "content": turn.content},
                    "finish_reason": turn.finish_reason or "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": turn.output_tokens if turn.output_tokens is not None else 8,
            },
        }
        return _HTTPResponse(json.dumps(payload))


def fence(path: str, content: str) -> str:
    return f"File: {path}\n```mjs\n{content}\n```\n"


def files_of(response):
    body = response.parsed_body or {}
    files = body.get("files") if hasattr(body, "get") else None
    return dict(files or {})


def missing_section(prompt: str) -> str:
    return prompt.split("Missing owned_files:", 1)[1].split("Owned scope", 1)[0]


def drive(kind: str, turns: list[Turn], owned: list[str], tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    metadata = {"artifact_delivery": True, "owned_files": list(owned)}
    request = ApiRequest(
        task_id="mod",
        idempotency_key="idem-mod",
        prompt="deliver the owned files",
        metadata=metadata,
    )
    if kind == "fake":
        steps = []
        for turn in turns:
            body = turn.raw_http if turn.raw_http is not None else turn.content
            usage = {
                "input_tokens": 4,
                "output_tokens": turn.output_tokens if turn.output_tokens is not None else 8,
            }
            steps.append(Step.success(body, usage=usage, finish_reason=turn.finish_reason))
        client = FakeClient(steps, raw_dir=raw_dir)
        response = client.call(request)
        return client, response, len(client.received_raw)
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    script = Script(turns)
    client = HttpClient(opener=script.opener, raw_dir=raw_dir)
    response = client.call(request)
    return client, response, script.calls


def _task(owned: list[str]) -> Task:
    return Task(
        id="mod",
        payload={"owned_files": list(owned)},
        constraints_declared=True,
    )


def test_recovery_rules_are_shared():
    assert allows_format_repair(ERROR_INVALID_FORMAT_NO_ARTIFACT)
    assert allows_format_repair(ERROR_PARTIAL_ARTIFACT)
    assert not allows_format_repair(ERROR_INVALID_JSON)
    assert not allows_format_repair(ERROR_OUTPUT_TRUNCATED)
    assert not allows_format_repair("timeout")
    assert recovery_for("rate_limited") == recovery_for("server_error") == "retry_transport"
    assert recovery_for(ERROR_INVALID_FORMAT_NO_ARTIFACT) == "format_repair_once"
    assert recovery_for(ERROR_PARTIAL_ARTIFACT) == "format_repair_once"
    assert recovery_for(ERROR_OUTPUT_TRUNCATED) == "none"


def test_merge_files_does_not_overwrite_completed():
    merged = merge_files(
        {"src/a.mjs": "ORIGINAL\n"},
        {"src/a.mjs": "CHANGED\n", "src/b.mjs": "NEW\n"},
    )
    assert merged["src/a.mjs"] == "ORIGINAL\n"
    assert merged["src/b.mjs"] == "NEW\n"


def test_deliver_file_keeps_completed_bytes(tmp_path):
    sink = _ArtifactSink(tmp_path)
    first = sink.deliver_file("src/a.mjs", "ORIGINAL\n")
    second = sink.deliver_file("src/a.mjs", "CHANGED\n")
    assert first["status"] == "delivered"
    assert second["status"] == "delivered"
    assert second["hash"] == first["hash"]
    assert (tmp_path / "src" / "a.mjs").read_text(encoding="utf-8") == "ORIGINAL\n"


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_normal_json_delivery_succeeds(kind, tmp_path, monkeypatch):
    content = json.dumps(
        {
            "ok": True,
            "files": {
                "src/a.mjs": "export const a = 1;\n",
                "src/b.mjs": "export const b = 1;\n",
            },
        }
    )
    client, response, calls = drive(
        kind,
        [Turn(content), Turn(fence("src/a.mjs", "SHOULD_NOT"))],
        OWNED,
        tmp_path,
        monkeypatch,
    )
    assert calls == 1
    assert response.ok
    assert response.error_type is None
    assert response.format_repair_used is False
    assert client.repair_prompts == []
    assert files_of(response)["src/a.mjs"] == "export const a = 1;\n"
    sink = _ArtifactSink(tmp_path / "ws")
    report = legacy_artifact_acceptor(sink=sink)(_task(OWNED), response)
    assert report.passed, report
    assert (tmp_path / "ws" / "src" / "a.mjs").read_text(encoding="utf-8") == "export const a = 1;\n"


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_json_fails_but_acs_extract_succeeds(kind, tmp_path, monkeypatch):
    content = fence("src/a.mjs", "export const a = 1;") + fence("src/b.mjs", "export const b = 2;")
    client, response, calls = drive(
        kind,
        [Turn(content), Turn("SHOULD_NOT")],
        OWNED,
        tmp_path,
        monkeypatch,
    )
    assert calls == 1
    assert response.ok
    assert response.error_type is None
    assert client.repair_prompts == []
    assert "export const a = 1;" in files_of(response)["src/a.mjs"]
    assert "export const b = 2;" in files_of(response)["src/b.mjs"]
    sink = _ArtifactSink(tmp_path / "ws")
    report = legacy_artifact_acceptor(sink=sink)(_task(OWNED), response)
    assert report.passed, report


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_json_fails_and_acs_extracts_nothing(kind, tmp_path, monkeypatch):
    first = "this is not json {{{"
    client, response, calls = drive(
        kind,
        [Turn(raw_http=first), Turn(raw_http="still not a delivery")],
        OWNED,
        tmp_path,
        monkeypatch,
    )
    assert calls == 2
    assert response.outcome is Outcome.INVALID_JSON
    assert response.error_type == ERROR_INVALID_FORMAT_NO_ARTIFACT
    assert response.retryable is False
    assert response.format_repair_used is True
    assert response.format_repair_allowed is False
    assert allows_format_repair(response.error_type)
    assert response.raw_body == first
    assert client.received_raw[0]["body"] == first
    assert client.received_raw[0]["phase"] == "before_parse"
    saved = Path(client.received_raw[0]["path"]).read_text(encoding="utf-8")
    assert saved == first
    assert files_of(response) == {}
    assert len(client.repair_prompts) == 1
    assert "ACS-recognizable" in client.repair_prompts[0]
    section = missing_section(client.repair_prompts[0])
    assert "src/a.mjs" in section and "src/b.mjs" in section


def test_http_persists_raw_before_any_json_parse(tmp_path, monkeypatch):
    monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
    raw_dir = tmp_path / "raw"
    marker = "BODY_BEFORE_PARSE"
    seen: list[int] = []
    import feasibility_scheduler.http_client as http_mod

    real_loads = http_mod.json.loads

    def tracking(text, *args, **kwargs):
        if isinstance(text, str) and marker in text:
            files = list(raw_dir.glob("*.raw.txt"))
            assert files, "Raw file missing before JSON parse"
            assert marker in files[0].read_text(encoding="utf-8")
            seen.append(1)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(http_mod.json, "loads", tracking)
    payload = {
        "id": "chatcmpl-order",
        "choices": [{"message": {"role": "assistant", "content": json.dumps({"ok": True})}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    body = json.dumps({"marker": marker, **payload})
    # marker must be inside the HTTP body that json.loads will parse.
    body = json.dumps(payload).replace("chatcmpl-order", marker)

    def opener(req, timeout=None):
        return _HTTPResponse(body)

    client = HttpClient(opener=opener, raw_dir=raw_dir)
    response = client.call(ApiRequest(task_id="t", idempotency_key="k", prompt="p"))
    assert seen, "json.loads was not observed"
    assert response.raw_body is not None
    assert client.received_raw[0]["phase"] == "before_parse"


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_partial_files_keep_completed_and_do_not_retry_twice(kind, tmp_path, monkeypatch):
    client, response, calls = drive(
        kind,
        [
            Turn(fence("src/a.mjs", "ORIGINAL")),
            Turn(fence("src/a.mjs", "CHANGED")),
            Turn(fence("src/b.mjs", "SENTINEL")),
        ],
        OWNED,
        tmp_path,
        monkeypatch,
    )
    assert calls == 2
    assert response.ok
    assert response.error_type == ERROR_PARTIAL_ARTIFACT
    assert response.format_repair_used is True
    assert response.format_repair_allowed is False
    assert "ORIGINAL" in files_of(response)["src/a.mjs"]
    assert "CHANGED" not in files_of(response)["src/a.mjs"]
    assert "src/b.mjs" not in files_of(response)
    assert len(client.repair_prompts) == 1
    section = missing_section(client.repair_prompts[0])
    assert "src/b.mjs" in section
    assert "src/a.mjs" not in section
    assert "File:" in client.repair_prompts[0]
    sink = _ArtifactSink(tmp_path / "ws")
    report = legacy_artifact_acceptor(sink=sink)(_task(OWNED), response)
    assert not report.passed
    assert "ORIGINAL" in (tmp_path / "ws" / "src" / "a.mjs").read_text(encoding="utf-8")
    assert not (tmp_path / "ws" / "src" / "b.mjs").exists()
    assert sink.delivery["src/b.mjs"]["status"] == "missing"


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_true_output_truncation_is_not_format_repaired(kind, tmp_path, monkeypatch):
    truncated = '{"ok":true,"files":{"src/a.mjs":"export const a ='
    client, response, calls = drive(
        kind,
        [
            Turn(truncated, finish_reason="length", output_tokens=65536),
            Turn(fence("src/a.mjs", "SHOULD_NOT_REPAIR") + fence("src/b.mjs", "NOPE")),
        ],
        OWNED,
        tmp_path,
        monkeypatch,
    )
    assert calls == 1
    assert response.outcome is Outcome.INVALID_JSON
    assert response.error_type == ERROR_OUTPUT_TRUNCATED
    assert response.retryable is False
    assert response.format_repair_used is False
    assert response.format_repair_allowed is False
    assert not allows_format_repair(response.error_type)
    assert client.repair_prompts == []
    assert response.raw_body == truncated
    assert client.received_raw[0]["phase"] == "before_parse"
    persisted = Path(client.received_raw[0]["path"]).read_text(encoding="utf-8")
    assert persisted == client.received_raw[0]["body"]
    assert "export const a =" in persisted
    assert files_of(response) == {}


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_format_repair_succeeds_and_keeps_original_raw(kind, tmp_path, monkeypatch):
    client, response, calls = drive(
        kind,
        [
            Turn("NOPE"),
            Turn(fence("src/a.mjs", "export const a = 1;") + fence("src/b.mjs", "export const b = 2;")),
            Turn(fence("src/a.mjs", "SHOULD_NOT")),
        ],
        OWNED,
        tmp_path,
        monkeypatch,
    )
    assert calls == 2
    assert response.ok
    assert response.error_type is None
    assert response.format_repair_used is True
    assert response.raw_body == "NOPE"
    assert "NOPE" in client.received_raw[0]["body"]
    assert "export const a = 1;" in files_of(response)["src/a.mjs"]
    assert "SHOULD_NOT" not in files_of(response)["src/a.mjs"]
    sink = _ArtifactSink(tmp_path / "ws")
    report = legacy_artifact_acceptor(sink=sink)(_task(OWNED), response)
    assert report.passed, report


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_format_repair_failure_stops(kind, tmp_path, monkeypatch):
    client, response, calls = drive(
        kind,
        [
            Turn("nope-1"),
            Turn("nope-2"),
            Turn(fence("src/a.mjs", "SHOULD_NOT") + fence("src/b.mjs", "SHOULD_NOT")),
        ],
        OWNED,
        tmp_path,
        monkeypatch,
    )
    assert calls == 2
    assert response.outcome is Outcome.INVALID_JSON
    assert response.error_type == ERROR_INVALID_FORMAT_NO_ARTIFACT
    assert response.format_repair_used is True
    assert response.format_repair_allowed is False
    assert response.retryable is False
    assert response.raw_body == "nope-1"
    assert len(client.repair_prompts) == 1
    assert "SHOULD_NOT" not in (response.raw_body or "")
    assert files_of(response) == {}
    if kind == "fake":
        assert client.script_remaining == 1


def test_acceptor_keeps_raw_when_format_has_no_artifact(tmp_path):
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    raw = "not a delivery at all"
    response = ApiResponse(
        request_id="r1",
        idempotency_key="k",
        task_id="mod",
        provider="fake",
        model="m",
        outcome=Outcome.INVALID_JSON,
        status_code=200,
        raw_body=raw,
        parsed_body=None,
        usage={"input_tokens": 1, "output_tokens": 1},
        error_type=ERROR_INVALID_FORMAT_NO_ARTIFACT,
        retry_after_seconds=None,
        is_duplicate=False,
        duplicate_of=None,
        attempt=1,
        started_at=now,
        finished_at=now,
        input_artifact_hash="sha256:abc",
        format_repair_used=True,
    )
    sink = _ArtifactSink(tmp_path)
    report = legacy_artifact_acceptor(sink=sink)(_task(OWNED), response)
    assert not report.passed
    assert any("invalid_format_no_artifact" in err for err in report.errors)
    audit = json.loads((tmp_path / "_raw_responses" / "mod.json").read_text(encoding="utf-8"))
    assert audit["raw_body"] == raw
    assert audit["error_type"] == ERROR_INVALID_FORMAT_NO_ARTIFACT


def test_legacy_executor_repairs_once_inside_the_adapter(tmp_path):
    """Mixin 把 owned_files 交给 client；内核重试循环不会再打第三次。"""
    from feasibility_scheduler.adapters.legacy_doc import PromptFromPayloadExecutor
    from feasibility_scheduler.events import EventLog
    from feasibility_scheduler.statemachine import TaskState
    from feasibility_scheduler.store import TaskStore

    owned = ["src/a.mjs"]
    task = Task(
        id="mod",
        constraints_declared=True,
        output_contract={"type": "object", "required": ["ok"]},
        payload={"owned_files": owned, "prompt_text": "deliver src/a.mjs"},
    )
    sink = _ArtifactSink(tmp_path / "ws")
    client = FakeClient(
        [
            Step.success("nope"),
            Step.success(fence("src/a.mjs", "export const a = 1;")),
            Step.success(fence("src/a.mjs", "SHOULD_NOT")),
        ]
    )
    store = TaskStore(tmp_path / "state.sqlite3")
    log = EventLog(tmp_path / "events.jsonl", run_id="repair-run")
    try:
        outcome = PromptFromPayloadExecutor(
            [task],
            store=store,
            log=log,
            client=client,
            acceptor=legacy_artifact_acceptor(sink=sink),
            max_concurrency=1,
        ).run()
    finally:
        store.close()
    assert outcome.states["mod"] is TaskState.ACCEPTED, outcome.verdict.reasons
    assert outcome.ok
    assert client.script_remaining == 1
    assert len(client.repair_prompts) == 1
    assert "src/a.mjs" in missing_section(client.repair_prompts[0])
    assert "export const a = 1;" in (tmp_path / "ws" / "src" / "a.mjs").read_text(encoding="utf-8")
    assert client.responses[0].raw_body == "nope"


@pytest.mark.parametrize("kind", ["fake", "http"])
def test_clients_agree_without_artifact_scope(kind, tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    request = ApiRequest(task_id="mod", idempotency_key="idem-plain", prompt="p")
    if kind == "fake":
        client = FakeClient([Step.success("not-json")], raw_dir=raw_dir)
        response = client.call(request)
        prompts = client.repair_prompts
    else:
        monkeypatch.setenv("FEASIBILITY_API_KEY", "sk-unit-test-key")
        script = Script([Turn("not-json"), Turn("SHOULD_NOT")])
        client = HttpClient(opener=script.opener, raw_dir=raw_dir)
        response = client.call(request)
        prompts = client.repair_prompts
        assert script.calls == 1
    assert response.error_type == ERROR_INVALID_FORMAT_NO_ARTIFACT
    assert response.retryable is False
    assert response.format_repair_used is False
    assert response.format_repair_allowed is True
    assert prompts == []
