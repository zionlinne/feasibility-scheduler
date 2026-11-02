import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from feasibility_scheduler.fake_client import ApiRequest, Outcome, RequestStatus, RequestValidationError
from feasibility_scheduler.native_client import NativeClient


def test_native_request_response_and_recovery_lookup(tmp_path):
    client = NativeClient(tmp_path, timeout_seconds=2)
    request = ApiRequest(task_id="module", idempotency_key="native-1", prompt="implement module")
    source, destination = client._paths(request.idempotency_key)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.call, request)
        # Simulate only the host rendezvous protocol, not a model result.
        import time
        deadline = time.monotonic() + 1
        while not source.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert json.loads(source.read_text())["input_artifact_hash"] == request.input_artifact_hash
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps({"producer": "test-host", "body": {"ok": True}}))
        temporary.replace(destination)
        response = pending.result()
    assert response.ok and response.provider == "codex-native" and response.usage == {}
    assert client.request_status("native-1") == RequestStatus.SENT
    assert NativeClient(tmp_path).lookup_response("native-1").parsed_body["ok"]
    with pytest.raises(RequestValidationError):
        client.call(ApiRequest(task_id="module", idempotency_key="native-1", prompt="different input"))


def test_native_timeout_preserves_unknown_request(tmp_path):
    client = NativeClient(tmp_path, timeout_seconds=0.01)
    response = client.call(ApiRequest(task_id="module", idempotency_key="slow", prompt="wait"))
    assert response.outcome == Outcome.TIMEOUT
    assert client.request_status("slow") == RequestStatus.UNKNOWN
    assert client.lookup_response("slow") is None
