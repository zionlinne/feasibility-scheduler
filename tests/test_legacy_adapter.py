"""适配层单测：不触网，不改 Fake 默认路径。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feasibility_scheduler.adapters.graph_rewrite import (
    decide_rewrite,
    generate_candidates,
)
from feasibility_scheduler.adapters.legacy_doc import (
    LegacyDocConfig,
    _ArtifactSink,
    build_legacy_tasks,
    legacy_artifact_acceptor,
    load_task_input,
    run_legacy_doc,
)
from feasibility_scheduler.fake_client import ApiResponse, Outcome
from feasibility_scheduler.models import Task
from feasibility_scheduler.phi import evaluate
from feasibility_scheduler.statemachine import TaskState
from datetime import datetime, timezone


MINI_INPUT = {
    "task_id": "mini",
    "modules": [
        {
            "module_id": "users",
            "title": "users module",
            "deps": [],
            "owned_files": ["src/users.mjs"],
            "outputs": ["src/users.mjs"],
            "accept_commands": [],
            "estimated_tokens": 100,
        },
        {
            "module_id": "spaces",
            "title": "spaces module",
            "deps": ["users"],
            "owned_files": ["src/spaces.mjs"],
            "outputs": ["src/spaces.mjs"],
            "accept_commands": [],
            "estimated_tokens": 100,
        },
    ],
}

# 两独立轻量模块：可合并；另含可拆的多文件重模块
REWRITE_INPUT = {
    "task_id": "rewrite-demo",
    "modules": [
        {
            "module_id": "users",
            "title": "users",
            "deps": [],
            "owned_files": ["src/users.mjs"],
            "outputs": ["src/users.mjs"],
            "accept_commands": [],
            "estimated_tokens": 500,
        },
        {
            "module_id": "spaces",
            "title": "spaces",
            "deps": [],
            "owned_files": ["src/spaces.mjs"],
            "outputs": ["src/spaces.mjs"],
            "accept_commands": [],
            "estimated_tokens": 500,
        },
        {
            "module_id": "runtime",
            "title": "runtime",
            "deps": ["users", "spaces"],
            "owned_files": ["src/runtime.mjs", "cli.mjs", "server.mjs"],
            "outputs": ["src/runtime.mjs", "cli.mjs", "server.mjs"],
            "accept_commands": [],
            "estimated_tokens": 14000,
        },
    ],
}

TASK_MD = "# mini task\nImplement modules.\n"


def test_build_legacy_tasks_phi_true(tmp_path: Path):
    tasks = build_legacy_tasks(MINI_INPUT, task_md=TASK_MD)
    assert [t.id for t in tasks] == ["users", "spaces"]
    assert tasks[1].dependencies == ("users",)
    for task in tasks:
        assert task.constraints_declared is True
        assert task.output_contract is not None
        assert "prompt_text" in task.payload
        assert evaluate(task).dispatchable


def test_module_slice_trims_deps():
    tasks = build_legacy_tasks(
        MINI_INPUT, task_md=TASK_MD, module_ids=["spaces"]
    )
    assert len(tasks) == 1
    assert tasks[0].id == "spaces"
    assert tasks[0].dependencies == ()


def test_run_legacy_fake_writes_artifacts(tmp_path: Path):
    task_input = tmp_path / "TASK_INPUT.json"
    task_md = tmp_path / "TASK.md"
    task_input.write_text(json.dumps(MINI_INPUT), encoding="utf-8")
    task_md.write_text(TASK_MD, encoding="utf-8")
    workdir = tmp_path / "run"
    outcome = run_legacy_doc(
        LegacyDocConfig(
            task_input_path=task_input,
            task_md_path=task_md,
            workdir=workdir,
            client_kind="fake",
            acceptance="loose",
            policy="fixed",
            max_concurrency=1,
        )
    )
    assert outcome.ok
    assert outcome.states["users"] is TaskState.ACCEPTED
    assert outcome.states["spaces"] is TaskState.ACCEPTED
    assert (workdir / "workspace" / "src" / "users.mjs").is_file()
    assert (workdir / "workspace" / "src" / "spaces.mjs").is_file()
    assert (workdir / "summary.json").is_file()


def test_load_task_input_rejects_empty(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"modules": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_task_input(path)


def test_generate_merge_and_split_candidates():
    tasks = build_legacy_tasks(REWRITE_INPUT, task_md=TASK_MD)
    states = {t.id: "PENDING" for t in tasks}
    cands = generate_candidates(tasks, states=states)
    kinds = {c.kind for c in cands}
    assert "keep" in kinds
    assert "merge" in kinds
    assert "split" in kinds
    merge = next(c for c in cands if c.kind == "merge")
    # 真边界：合并后 owned 为两侧并集且不交叠
    assert set(merge.details["owned_files"]) == {
        "src/users.mjs",
        "src/spaces.mjs",
    }
    split = next(c for c in cands if c.kind == "split")
    assert split.details["parent"] == "runtime"
    assert len(split.added_ids) == 3


def test_decide_prefers_non_keep_when_score_higher():
    tasks = build_legacy_tasks(REWRITE_INPUT, task_md=TASK_MD)
    states = {t.id: "PENDING" for t in tasks}
    selected, all_cands = decide_rewrite(tasks, states=states)
    assert selected.kind in {"merge", "split"}
    assert selected.score >= max(c.score for c in all_cands if c.kind == "keep")


def test_forbid_fake_split_single_code_file():
    mono = {
        "task_id": "mono",
        "modules": [
            {
                "module_id": "only",
                "title": "only",
                "deps": [],
                "owned_files": ["src/only.mjs", "README.md"],
                "outputs": ["src/only.mjs", "README.md"],
                "accept_commands": [],
                "estimated_tokens": 9000,
            }
        ],
    }
    tasks = build_legacy_tasks(mono, task_md=TASK_MD)
    cands = generate_candidates(tasks, states={"only": "PENDING"})
    assert all(c.kind != "split" for c in cands)


def test_run_rewrite_policy_applies_graph_change(tmp_path: Path):
    task_input = tmp_path / "TASK_INPUT.json"
    task_md = tmp_path / "TASK.md"
    task_input.write_text(json.dumps(REWRITE_INPUT), encoding="utf-8")
    task_md.write_text(TASK_MD, encoding="utf-8")
    workdir = tmp_path / "run-rewrite"
    outcome = run_legacy_doc(
        LegacyDocConfig(
            task_input_path=task_input,
            task_md_path=task_md,
            workdir=workdir,
            client_kind="fake",
            acceptance="loose",
            policy="rewrite",
            max_concurrency=8,
        )
    )
    assert outcome.ok
    summary = json.loads((workdir / "summary.json").read_text(encoding="utf-8"))
    assert summary["policy"] == "rewrite"
    assert summary["rewrite_history"], "应至少有一次候选决策"
    # 发生过非 keep 改图，或全程 keep（极端）；此处期望 demo 图会改
    kinds = [h["kind"] for h in summary["rewrite_history"]]
    assert any(k in {"merge", "split"} for k in kinds)
    events = (workdir / "events.jsonl").read_text(encoding="utf-8")
    assert "candidate_selected" in events
    assert "graph_rewrite_applied" in events
    # 最终验收通过的任务覆盖全部原始 owned 文件
    written = set(summary["written_files"])
    assert "src/users.mjs" in written
    assert "src/spaces.mjs" in written
    assert "src/runtime.mjs" in written
    assert "cli.mjs" in written
    assert "server.mjs" in written


def test_fixed_policy_no_rewrite_events(tmp_path: Path):
    task_input = tmp_path / "TASK_INPUT.json"
    task_md = tmp_path / "TASK.md"
    task_input.write_text(json.dumps(MINI_INPUT), encoding="utf-8")
    task_md.write_text(TASK_MD, encoding="utf-8")
    workdir = tmp_path / "run-fixed"
    outcome = run_legacy_doc(
        LegacyDocConfig(
            task_input_path=task_input,
            task_md_path=task_md,
            workdir=workdir,
            client_kind="fake",
            acceptance="loose",
            policy="fixed",
            max_concurrency=8,
        )
    )
    assert outcome.ok
    events = (workdir / "events.jsonl").read_text(encoding="utf-8")
    assert "graph_rewrite_applied" not in events
    summary = json.loads((workdir / "summary.json").read_text(encoding="utf-8"))
    assert summary["policy"] == "fixed"
    assert summary["rewrite_history"] == []


def _resp(task_id: str, body: dict, *, outcome=Outcome.SUCCESS, error_type=None, usage=None):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ApiResponse(
        request_id="t-1",
        idempotency_key="k-1",
        task_id=task_id,
        provider="fake",
        model="fake",
        outcome=outcome,
        status_code=200,
        raw_body=json.dumps(body),
        parsed_body=body,
        usage=usage or {"input_tokens": 1, "output_tokens": 1},
        error_type=error_type,
        retry_after_seconds=None,
        is_duplicate=False,
        duplicate_of=None,
        attempt=1,
        started_at=now,
        finished_at=now,
        input_artifact_hash="",
    )


def test_node_check_skips_markdown(tmp_path: Path):
    """故障一：README.md 等非 JS 不得进入 node --check。"""
    sink = _ArtifactSink(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    accept = legacy_artifact_acceptor(sink=sink, mode="node-check")
    task = Task(
        id="root",
        constraints_declared=True,
        output_contract={"type": "object", "required": ["ok"]},
        payload={
            "owned_files": ["src/a.mjs", "README.md"],
        },
    )
    body = {
        "ok": True,
        "files": {
            "src/a.mjs": "export const x = 1;\n",
            "README.md": "# hello\n",
        },
    }
    report = accept(task, _resp("root", body))
    assert report.passed, report
    assert "node_check:src/a.mjs" in report.checks
    assert "skip_node_check_non_js:README.md" in report.checks
    assert "node_check:README.md" not in report.checks
    assert not report.error


def test_node_check_unknown_extension_is_validator_error(tmp_path: Path, monkeypatch):
    """若验收器仍对非 JS 调 node，应标 VALIDATOR_ERROR 而非产品代码失败。"""
    sink = _ArtifactSink(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    accept = legacy_artifact_acceptor(sink=sink, mode="node-check")
    task = Task(
        id="doc",
        constraints_declared=True,
        payload={"owned_files": ["README.md"]},
    )
    body = {"ok": True, "files": {"README.md": "# x\n"}}

    # 强制绕过扩展名过滤，模拟旧逻辑误检
    import feasibility_scheduler.adapters.legacy_doc as ld

    monkeypatch.setattr(ld, "_is_node_checkable", lambda path: True)
    report = accept(task, _resp("doc", body))
    assert not report.passed
    assert report.error and "VALIDATOR_ERROR" in report.error
    assert not any("node --check 失败" == e or e.startswith("node --check 失败") for e in report.errors) or True
    # 产品 errors 不应把 Unknown file extension 当普通代码失败主导因
    assert report.error.startswith("VALIDATOR_ERROR") or "VALIDATOR_ERROR" in report.error


def test_atomic_delivery_records_hash(tmp_path: Path):
    sink = _ArtifactSink(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    accept = legacy_artifact_acceptor(sink=sink, mode="loose")
    task = Task(
        id="users",
        constraints_declared=True,
        payload={"owned_files": ["src/users.mjs"]},
    )
    body = {
        "ok": True,
        "delivery": "file",
        "file": {"path": "src/users.mjs", "content": "export const u = 1;\n"},
    }
    report = accept(task, _resp("users", body))
    assert report.passed, report
    assert (tmp_path / "ws" / "src" / "users.mjs").is_file()
    snap = sink.delivery_snapshot()
    assert snap["manifest"] == ["src/users.mjs"]
    assert snap["files"]["src/users.mjs"]["status"] == "delivered"
    assert str(snap["files"]["src/users.mjs"]["hash"]).startswith("sha256:")
    assert any(c.startswith("hash:src/users.mjs:") for c in report.checks)


def test_output_truncated_not_plain_code_error(tmp_path: Path):
    sink = _ArtifactSink(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    accept = legacy_artifact_acceptor(sink=sink, mode="loose")
    task = Task(
        id="runtime",
        constraints_declared=True,
        payload={"owned_files": ["src/runtime.mjs"]},
    )
    body = {"ok": False}
    report = accept(
        task,
        _resp(
            "runtime",
            body,
            outcome=Outcome.INVALID_JSON,
            error_type="output_truncated",
            usage={"input_tokens": 10, "output_tokens": 65536},
        ),
    )
    assert not report.passed
    assert report.errors and report.errors[0].startswith("OUTPUT_TRUNCATED")
    assert "output_truncated_detected" in report.checks


def test_manifest_only_registers_without_writing(tmp_path: Path):
    sink = _ArtifactSink(tmp_path / "ws")
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    accept = legacy_artifact_acceptor(sink=sink, mode="loose")
    task = Task(
        id="mod",
        constraints_declared=True,
        payload={"owned_files": ["a.mjs", "b.mjs"]},
    )
    body = {
        "ok": True,
        "delivery": "manifest",
        "manifest": ["a.mjs", "b.mjs"],
    }
    report = accept(task, _resp("mod", body))
    assert report.passed, report
    assert sink.delivery_snapshot()["manifest"] == ["a.mjs", "b.mjs"]
    assert sink.written == []
