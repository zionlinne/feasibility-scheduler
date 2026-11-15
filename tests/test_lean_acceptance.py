"""Unit and optional real-Lean checks of the FBS acceptance adapter."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from feasibility_scheduler.acceptance import run_acceptance
from feasibility_scheduler.fake_client import ApiRequest, FakeClient, Step
from feasibility_scheduler.lean_acceptance import make_lean_acceptor
from feasibility_scheduler.models import Task


def response(proof="by rfl"):
    from json import dumps

    client = FakeClient([Step.success(dumps({"proof": proof}))])
    return client.call(
        ApiRequest(task_id="math", idempotency_key="k", prompt="prove")
    )


def test_valid_proof_and_goal_is_fixed(monkeypatch):
    def run(args, **kwargs):
        source = Path(args[-1]).read_text(encoding="utf-8")
        assert "theorem fbs_goal : (1 + 1 : Nat) = 2 := by rfl" in source
        assert "#print axioms fbs_goal" in source
        return subprocess.CompletedProcess(
            args, 0, "'fbs_goal' does not depend on any axioms", ""
        )

    monkeypatch.setattr(subprocess, "run", run)
    acceptor = make_lean_acceptor(goal_statement="(1 + 1 : Nat) = 2")
    report = run_acceptance(acceptor, Task(id="math"), response())
    assert report and report.passed


@pytest.mark.parametrize(
    "axioms", ["sorryAx", "Custom.bad", "propext, sorryAx"]
)
def test_rejects_nonstandard_axioms(monkeypatch, axioms):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, f"'fbs_goal' depends on axioms: [{axioms}]", ""
        ),
    )
    report = make_lean_acceptor(goal_statement="True")(
        Task(id="math"), response()
    )
    assert not report.passed
    assert "Nonstandard axioms" in report.errors[0]


def test_rejects_missing_axiom_output(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
    )
    report = make_lean_acceptor(goal_statement="True")(
        Task(id="math"), response()
    )
    assert not report.passed


def test_rejects_lean_proof_error(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, "", "type mismatch"
        ),
    )
    report = make_lean_acceptor(goal_statement="True")(
        Task(id="math"), response("by exact False.elim")
    )
    assert not report.passed and "Lean rejected" in report.errors[0]


def test_rejects_multiline_response(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("must not run Lean")
    )
    report = make_lean_acceptor(goal_statement="True")(
        Task(id="math"), response("by rfl\naxiom cheated : False")
    )
    assert not report.passed


def test_lean_not_installed_fails_closed(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("lean")

    monkeypatch.setattr(subprocess, "run", missing)
    report = make_lean_acceptor(goal_statement="True")(
        Task(id="math"), response()
    )
    assert not report.passed and "Lean unavailable" in report.error


def test_timeout_fails_closed(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="lean", timeout=2)

    monkeypatch.setattr(subprocess, "run", timeout)
    report = make_lean_acceptor(goal_statement="True")(
        Task(id="math"), response()
    )
    assert not report.passed and "timed out" in report.error


@pytest.mark.skipif(shutil.which("lean") is None, reason="Lean 4 not installed")
def test_real_lean_acceptance_smoke():
    """Run against the real Lean kernel if the binary is installed."""
    acceptor = make_lean_acceptor(goal_statement="(1 + 1 : Nat) = 2")
    assert acceptor(Task(id="math"), response("by rfl")).passed
    assert not acceptor(Task(id="math"), response("by sorry")).passed


def test_executor_uses_lean_acceptor_without_changing_dispatch(monkeypatch, tmp_path):
    """End-to-end FBS integration with mocked Lean: READY -> ACCEPTED."""
    from feasibility_scheduler.events import EventLog
    from feasibility_scheduler.executor import Executor
    from feasibility_scheduler.fake_client import FakeClient
    from feasibility_scheduler.statemachine import TaskState
    from feasibility_scheduler.store import TaskStore

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, "'fbs_goal' does not depend on any axioms", ""
        ),
    )
    task = Task(
        id="math",
        constraints_declared=True,
        # FBS's existing graph validator requires a declared output contract,
        # even when the custom acceptor is used instead of contract_acceptor.
        output_contract={"type": "object"},
        payload={"work": "Prove the fixed Lean theorem"},
    )
    store = TaskStore()
    try:
        log = EventLog(tmp_path / "lean-events.jsonl", run_id="lean-test")
        outcome = Executor(
            [task],
            store=store,
            log=log,
            client=FakeClient([Step.success('{"proof": "by rfl"}')]),
            acceptor=make_lean_acceptor(goal_statement="(1 + 1 : Nat) = 2"),
        ).run()
        assert outcome.ok
        assert outcome.states["math"] is TaskState.ACCEPTED
        assert outcome.reports["math"].passed
        assert any(event.type == "acceptance_passed" for event in log.events())
    finally:
        store.close()
