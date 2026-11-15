"""Experimental Lean 4 final-acceptance adapter for FBS.

The theorem statement must be supplied by trusted task configuration, not by
the model response. This module only replaces the acceptor; it does not modify
FBS feasibility checks, dependency scheduling or event logging.

SECURITY: Lean has executable metaprogramming. This prototype is for trusted
experiments only and does not sandbox untrusted model-generated code.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from .acceptance import AcceptanceReport, Acceptor
from .fake_client import ApiResponse
from .models import Task

_NAME = "fbs_goal"
_ALLOWED_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
_MAX_PROOF_LENGTH = 16384


def make_lean_acceptor(
    *,
    goal_statement: str,
    lean_executable: str = "lean",
    timeout_seconds: float = 20.0,
    lean_import: str = "Init",
) -> Acceptor:
    """Make a custom Executor acceptor for a fixed Lean proposition.

    Model response format: {"proof": "by rfl"}. The model may submit a proof
    expression but may NOT change the proposition to be proved.
    This first experiment accepts only one-line expressions.
    """
    if not isinstance(goal_statement, str) or not goal_statement.strip():
        raise ValueError("goal_statement must be nonempty")
    if "\n" in goal_statement or "\r" in goal_statement:
        raise ValueError("goal_statement must be a single trusted proposition")
    if not isinstance(lean_executable, str) or not lean_executable.strip():
        raise ValueError("lean_executable must be set")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", lean_import):
        raise ValueError("lean_import must name a trusted Lean module")
    goal = goal_statement.strip()

    def accept(task: Task, response: ApiResponse) -> AcceptanceReport:
        checks = ("response_ok", "proof_present", "lean_check", "axioms_checked")
        if response.task_id != task.id or not response.ok:
            return AcceptanceReport(
                task.id, False, checks, ("Task mismatch or provider failure",)
            )
        body = response.parsed_body
        if not isinstance(body, Mapping):
            return AcceptanceReport(
                task.id, False, checks, ("Response must be a JSON object",)
            )
        proof = body.get("proof")
        if not isinstance(proof, str) or not proof.strip():
            return AcceptanceReport(
                task.id, False, checks, ("Missing nonempty 'proof' field",)
            )
        proof = proof.strip()
        if (
            len(proof) > _MAX_PROOF_LENGTH
            or "\n" in proof or "\r" in proof or "\x00" in proof
        ):
            return AcceptanceReport(
                task.id, False, checks,
                ("Proof must be a single-line Lean expression of bounded length",),
            )

        source = (
            f"import {lean_import}\n"
            "set_option autoImplicit false\n"
            f"theorem {_NAME} : {goal} := {proof}\n"
            f"#print axioms {_NAME}\n"
        )
        try:
            with tempfile.TemporaryDirectory(prefix="fbs-lean-") as tmp:
                path = Path(tmp) / "FbsProof.lean"
                path.write_text(source, encoding="utf-8")
                result = subprocess.run(
                    [lean_executable, str(path)],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return AcceptanceReport(
                task.id, False, checks, error=f"Lean unavailable: {exc}"
            )
        except subprocess.TimeoutExpired:
            return AcceptanceReport(
                task.id, False, checks, error="Lean verification timed out"
            )

        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0:
            return AcceptanceReport(
                task.id, False, checks,
                ("Lean rejected proof: " + output.strip()[:1200],),
            )
        axioms = _parse_axioms(output)
        if axioms is None:
            return AcceptanceReport(
                task.id, False, checks,
                ("Could not confirm theorem axioms from Lean output",),
            )
        nonstandard = sorted(axioms - _ALLOWED_AXIOMS)
        if nonstandard:
            return AcceptanceReport(
                task.id, False, checks,
                ("Nonstandard axioms used: " + ", ".join(nonstandard),),
            )
        return AcceptanceReport(task.id, True, checks)

    return accept


def _parse_axioms(output: str) -> frozenset[str] | None:
    """Fail closed unless Lean explicitly attests the fixed theorem's axioms."""
    prefix = rf"(?:['`])?{re.escape(_NAME)}(?:['`])?"
    pattern = prefix + r"\s+(does not depend on any axioms|depends on axioms:\s*\[([^\]]*)\])"
    matches = list(re.finditer(pattern, output))
    if len(matches) != 1:  # Missing or conflicting declarations fail closed.
        return None
    match = matches[0]
    if match.group(2) is None:
        return frozenset()
    return frozenset(a.strip() for a in match.group(2).split(",") if a.strip())
