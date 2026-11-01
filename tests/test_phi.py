"""phi.py 的三值门禁测试：TRUE / FALSE / UNKNOWN 与派发门禁。"""

from dataclasses import FrozenInstanceError

import pytest

from feasibility_scheduler.models import Constraint, Task
from feasibility_scheduler.phi import (
    ACTION_DISPATCH,
    ACTION_REJECT,
    ACTION_REQUEST_INFO,
    UNKNOWN_POLICY,
    ConstraintCheck,
    FeasibilityError,
    Truth,
    Verdict,
    evaluate,
    require_dispatchable,
)


def make_task(constraints=(), constraints_declared=True, task_id="task"):
    return Task(
        id=task_id,
        constraints=tuple(constraints),
        constraints_declared=constraints_declared,
        output_contract={"type": "object"},
        payload={"work": True},
    )


def satisfied(name, source="schema-check"):
    return ConstraintCheck(name=name, verdict=Verdict.SATISFIED, source=source)


def test_all_satisfied_constraints_is_true():
    task = make_task([Constraint("budget<=10"), Constraint("files<=3")])

    report = evaluate(
        task,
        [satisfied("budget<=10", "budget-validator"), satisfied("files<=3", "owner-check")],
    )

    assert report.truth is Truth.TRUE
    assert report.dispatchable is True
    assert report.next_action == ACTION_DISPATCH
    assert [finding.name for finding in report.findings] == ["budget<=10", "files<=3"]
    assert all(finding.verdict is Verdict.SATISFIED for finding in report.findings)


def test_violated_necessary_constraint_is_false():
    task = make_task([Constraint("budget<=10"), Constraint("files<=3")])

    report = evaluate(
        task,
        [
            ConstraintCheck("budget<=10", Verdict.VIOLATED, "budget-validator"),
            satisfied("files<=3"),
        ],
    )

    assert report.truth is Truth.FALSE
    assert report.dispatchable is False
    assert report.next_action == ACTION_REJECT
    assert any("budget<=10" in reason for reason in report.reasons)


def test_missing_necessary_information_is_unknown():
    task = make_task([Constraint("budget<=10"), Constraint("files<=3")])

    report = evaluate(task, [satisfied("files<=3")])

    assert report.truth is Truth.UNKNOWN
    assert report.dispatchable is False
    assert report.next_action == ACTION_REQUEST_INFO
    missing = {finding.name: finding for finding in report.findings}["budget<=10"]
    assert missing.verdict is Verdict.UNKNOWN
    assert "budget<=10" in " ".join(report.reasons)


def test_explicitly_empty_constraints_is_true():
    task = make_task([], constraints_declared=True)

    report = evaluate(task, [])

    assert report.truth is Truth.TRUE
    assert report.dispatchable is True
    assert report.findings == ()
    assert "没有额外硬约束" in " ".join(report.reasons)


def test_omitted_constraint_field_is_unknown():
    task = make_task([], constraints_declared=False)

    report = evaluate(task, [])

    assert report.truth is Truth.UNKNOWN
    assert report.dispatchable is False
    assert report.next_action == ACTION_REQUEST_INFO
    assert "缺省" in " ".join(report.reasons)


@pytest.mark.parametrize(
    ("constraints", "declared", "checks", "expected"),
    [
        ([Constraint("a")], True, [satisfied("a")], Truth.TRUE),
        ([Constraint("a")], True, [ConstraintCheck("a", Verdict.VIOLATED, "violated-check")], Truth.FALSE),
        ([Constraint("a")], True, [], Truth.UNKNOWN),
        ([], True, [], Truth.TRUE),
        ([], False, [], Truth.UNKNOWN),
    ],
    ids=[
        "all-satisfied",
        "necessary-violated",
        "necessary-info-missing",
        "explicit-empty",
        "field-omitted",
    ],
)
def test_five_acceptance_scenarios(constraints, declared, checks, expected):
    """验收表：全部满足 TRUE / 违反 FALSE / 信息缺失 UNKNOWN / 显式空 TRUE / 字段缺省 UNKNOWN。"""
    task = make_task(constraints, constraints_declared=declared)

    assert evaluate(task, checks).truth is expected


def test_unknown_is_not_dispatchable_by_default():
    task = make_task([Constraint("budget<=10")])
    report = evaluate(task, [])

    assert report.truth is Truth.UNKNOWN
    assert UNKNOWN_POLICY == ACTION_REQUEST_INFO
    with pytest.raises(FeasibilityError) as excinfo:
        require_dispatchable(task, [])

    assert excinfo.value.report.truth is Truth.UNKNOWN
    assert "request-more-information" in str(excinfo.value)


def test_false_is_not_dispatchable():
    task = make_task([Constraint("budget<=10")])

    with pytest.raises(FeasibilityError) as excinfo:
        require_dispatchable(task, [ConstraintCheck("budget<=10", Verdict.VIOLATED, "budget")])

    assert excinfo.value.report.truth is Truth.FALSE
    assert excinfo.value.report.next_action == ACTION_REJECT


def test_true_passes_the_dispatch_gate():
    task = make_task([Constraint("budget<=10")])

    assert require_dispatchable(task, [satisfied("budget<=10")]).truth is Truth.TRUE


def test_violation_dominates_missing_evidence():
    task = make_task([Constraint("a"), Constraint("b")])

    report = evaluate(task, [ConstraintCheck("a", Verdict.VIOLATED, "check-a")])

    assert report.truth is Truth.FALSE


def test_undeclared_evidence_makes_the_input_inconsistent():
    task = make_task([Constraint("a")])

    report = evaluate(task, [satisfied("a"), satisfied("z")])

    assert report.truth is Truth.UNKNOWN
    assert "z" in " ".join(report.reasons)


def test_unrecognized_verdict_token_is_unknown():
    task = make_task([Constraint("a")])

    report = evaluate(task, [ConstraintCheck("a", "MAYBE", "informal-claim")])

    assert report.truth is Truth.UNKNOWN
    assert report.findings[0].verdict is Verdict.UNKNOWN


def test_boolean_evidence_is_not_treated_as_satisfied():
    task = make_task([Constraint("a")])

    report = evaluate(task, [ConstraintCheck("a", True, "model-claim")])

    assert report.truth is Truth.UNKNOWN


def test_conflicting_evidence_prefers_violation():
    task = make_task([Constraint("a")])

    report = evaluate(
        task,
        [ConstraintCheck("a", Verdict.SATISFIED, "fast-check"), ConstraintCheck("a", Verdict.VIOLATED, "strict-check")],
    )

    assert report.truth is Truth.FALSE
    assert any("冲突证据" in reason for reason in report.reasons)


def test_findings_record_evidence_source_and_are_sorted():
    task = make_task([Constraint("beta"), Constraint("alpha")])

    report = evaluate(task, [satisfied("alpha", "alpha-validator")])

    assert [finding.name for finding in report.findings] == ["alpha", "beta"]
    assert report.findings[0].evidence == "alpha-validator"
    assert report.findings[1].evidence == "没有可验证的证据"


def test_violation_is_reported_even_when_constraint_field_is_undeclared():
    task = make_task([Constraint("a")], constraints_declared=False)

    report = evaluate(task, [ConstraintCheck("a", Verdict.VIOLATED, "check-a")])

    assert report.truth is Truth.FALSE


def test_report_is_frozen_and_describable():
    task = make_task([Constraint("a")])
    report = evaluate(task, [satisfied("a")])

    assert "Φ=TRUE" in report.describe()
    with pytest.raises(FrozenInstanceError):
        report.truth = Truth.FALSE
