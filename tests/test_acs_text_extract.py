# -*- coding: utf-8 -*-
"""ACS text extract + truncated delivery keep-complete-files."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from feasibility_scheduler.adapters.acs_text_extract import (
    extract_delivery_map,
    extract_files,
)
from feasibility_scheduler.adapters.legacy_doc import (
    _ArtifactSink,
    legacy_artifact_acceptor,
)
from feasibility_scheduler.fake_client import ApiResponse, Outcome
from feasibility_scheduler.http_client import HttpClient
from feasibility_scheduler.models import Task


def _task(owned: list[str]) -> Task:
    return Task(
        id="runtime",
        payload={"owned_files": owned, "outputs": owned},
        constraints_declared=True,
        constraints=(),
        dependencies=(),
    )


def _resp(*, raw: str, parsed, error_type: str | None, ok: bool, tokens: int) -> ApiResponse:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ApiResponse(
        request_id="t1",
        idempotency_key="k1",
        task_id="runtime",
        provider="test",
        model="m",
        outcome=Outcome.SUCCESS if ok else Outcome.INVALID_JSON,
        status_code=200,
        raw_body=raw,
        parsed_body=parsed,
        usage={"input_tokens": 10, "output_tokens": tokens},
        error_type=error_type,
        retry_after_seconds=None,
        is_duplicate=False,
        duplicate_of=None,
        attempt=1,
        started_at=now,
        finished_at=now,
        input_artifact_hash="",
    )


def test_acs_fence_extract_matches_round2_a():
    answer = (
        "File: src/runtime.mjs\n```js\nexport const x=1;\n```\n"
        "File: `server.mjs`\n```javascript\nexport default {};\n```\n"
    )
    files = extract_files(answer)
    assert "src/runtime.mjs" in files
    assert "server.mjs" in files
    assert "export const x=1;" in files["src/runtime.mjs"]


def test_truncated_json_keeps_complete_files_and_marks_missing(tmp_path: Path):
    """65536-style cut: two files closed, third cut mid-string."""
    complete_a = "export const a = 1;\n" + ("// pad\n" * 50)
    complete_b = "export default { listen() {} };\n" + ("// pad\n" * 50)
    # Truncate mid third file content (no closing quote)
    raw = (
        '{"ok":true,"delivery":"files","files":{'
        + json.dumps("src/runtime.mjs")
        + ":"
        + json.dumps(complete_a)
        + ","
        + json.dumps("server.mjs")
        + ":"
        + json.dumps(complete_b)
        + ","
        + json.dumps("cli.mjs")
        + ':"export const cli = '
    )
    assert not raw.endswith("}")
    recovered = extract_delivery_map(raw)
    assert "src/runtime.mjs" in recovered["files"]
    assert "server.mjs" in recovered["files"]
    assert "cli.mjs" not in recovered["files"]

    sink = _ArtifactSink(workspace=tmp_path)
    accept = legacy_artifact_acceptor(sink=sink, mode="loose")
    owned = ["src/runtime.mjs", "server.mjs", "cli.mjs"]
    report = accept(
        _task(owned),
        _resp(raw=raw, parsed=None, error_type="output_truncated", ok=False, tokens=65536),
    )
    assert report.passed is False
    assert any("OUTPUT_TRUNCATED" in e for e in report.errors)
    assert (tmp_path / "src/runtime.mjs").is_file()
    assert (tmp_path / "server.mjs").is_file()
    assert not (tmp_path / "cli.mjs").exists()
    assert sink.delivery["src/runtime.mjs"]["status"] == "delivered"
    assert sink.delivery["server.mjs"]["status"] == "delivered"
    assert sink.delivery["cli.mjs"]["status"] == "OUTPUT_TRUNCATED"
    # raw audit kept
    assert (tmp_path / "_raw_responses" / "runtime.json").is_file()


def test_server_mjs_independent_delivery(tmp_path: Path):
    sink = _ArtifactSink(workspace=tmp_path)
    accept = legacy_artifact_acceptor(sink=sink, mode="loose")
    body = {"ok": True, "delivery": "file", "file": {"path": "server.mjs", "content": " consoleserver();"}}
    # fix content
    body = {"ok": True, "delivery": "file", "file": {"path": "server.mjs", "content": "export default {};\n"}}
    report = accept(
        _task(["server.mjs"]),
        _resp(raw=json.dumps(body), parsed=body, error_type=None, ok=True, tokens=100),
    )
    assert report.passed is True
    assert (tmp_path / "server.mjs").read_text(encoding="utf-8") == "export default {};\n"


def test_cli_server_merge_owned_only(tmp_path: Path):
    sink = _ArtifactSink(workspace=tmp_path)
    accept = legacy_artifact_acceptor(sink=sink, mode="loose")
    body = {
        "ok": True,
        "delivery": "files",
        "files": {
            "cli.mjs": "export const cli=1;\n",
            "server.mjs": "export const server=1;\n",
            "evil.mjs": "should not land",
        },
    }
    report = accept(
        _task(["cli.mjs", "server.mjs"]),
        _resp(raw=json.dumps(body), parsed=body, error_type=None, ok=True, tokens=200),
    )
    assert report.passed is False  # pollution error
    assert (tmp_path / "cli.mjs").exists() or "交付了非本模块文件 evil.mjs" in report.errors
    # owned may still be written depending on order; evil must not
    assert not (tmp_path / "evil.mjs").exists()


def test_http_client_parse_attaches_acs_partial():
    complete = "export const ok=1;\n"
    content = '{"ok":true,"files":{"server.mjs":' + json.dumps(complete) + ',"cli.mjs":"export con'
    parsed, failed = HttpClient._parse_artifact(content)
    assert failed is True
    recovered = extract_delivery_map(content)
    assert recovered["files"].get("server.mjs") == complete
    assert "cli.mjs" not in recovered["files"]
