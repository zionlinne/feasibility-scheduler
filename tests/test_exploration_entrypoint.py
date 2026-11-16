"""Exercise the experiment's actual file exchange, without an API/model."""

import asyncio
import json
import runpy
from pathlib import Path

import pytest

from feasibility_scheduler.client import ApiRequest, Outcome
from feasibility_scheduler.models import Task

entry = runpy.run_path(str(Path(__file__).parents[1] / "scripts/run_exploration_experiment.py"))


@pytest.mark.parametrize("stale", [False, True])
def test_file_exchange_binds_dispatch_and_forwards_actual_accepted_outputs(tmp_path, stale):
    for name in ("requests", "artifacts"):
        (tmp_path / name).mkdir()
    prior = {"analysis": {"report": "Actually accepted analysis, with unresolved alternatives."}}
    task = Task("synthesis", dependencies=("analysis",), payload={"question": "What follows?"})
    client = entry["FileWorkerClient"](tmp_path, [task], prior, 2)
    request = ApiRequest(task_id="synthesis", idempotency_key="current-attempt", prompt="synthesize")

    async def author():
        path = tmp_path / "requests/synthesis.json"
        while not path.exists():
            await asyncio.sleep(0.01)
        packet = json.loads(path.read_text())
        assert packet["accepted_dependencies"] == prior
        assert packet["input_artifact_hash"] != request.input_artifact_hash
        artifact = {"idempotency_key": "stale-attempt" if stale else packet["idempotency_key"],
                    "input_artifact_hash": packet["input_artifact_hash"],
                    "artifact": {"report": "Tentative synthesis, not a definitive answer."}}
        entry["write_json"](tmp_path / "artifacts/synthesis.json", artifact)

    async def exercise():
        result, _ = await asyncio.gather(client.acall(request), author())
        return result

    result = asyncio.run(exercise())
    assert result.outcome is (Outcome.INVALID_JSON if stale else Outcome.SUCCESS)
    if not stale:
        assert "Tentative synthesis" in result.parsed_body["report"]


def test_missing_artifact_times_out_instead_of_being_generated(tmp_path):
    for name in ("requests", "artifacts"):
        (tmp_path / name).mkdir()
    client = entry["FileWorkerClient"](tmp_path, [Task("analysis")], {}, 0.01)
    result = asyncio.run(client.acall(ApiRequest(task_id="analysis", idempotency_key="k", prompt="p")))
    assert result.outcome is Outcome.TIMEOUT
    assert result.parsed_body is None
