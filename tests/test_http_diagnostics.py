import json
from urllib.error import URLError

import pytest

from feasibility_scheduler.fake_client import ApiRequest, Outcome, RequestStatus
from feasibility_scheduler.http_client import HttpClient
from test_http_client import make_opener, openai_success


@pytest.mark.parametrize("failure", [ConnectionResetError("connection lost unit-secret-key"),
                                    URLError("TLS handshake failed unit-secret-key"), TimeoutError("read timed out")])
def test_transport_exception_survives_event_and_disk_audits(tmp_path, failure):
    client = HttpClient(api_key="unit-secret-key", raw_dir=tmp_path, opener=make_opener(error=failure))
    request = ApiRequest(task_id="one", idempotency_key="one", prompt="private user prompt")
    response = client.call(request)
    audit = response.to_audit_dict()
    assert audit["diagnostics"]["stage"] == "transport"
    assert audit["diagnostics"]["exception_type"] == type(failure).__name__
    assert response.status_code == 0
    expected = Outcome.TIMEOUT if isinstance(failure, TimeoutError) else Outcome.SERVER_ERROR
    assert response.outcome is expected
    assert response.error_type == ("timeout" if expected is Outcome.TIMEOUT else "transport_error")
    files = list(tmp_path.glob("*failure*"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "unit-secret-key" not in text + json.dumps(audit)
    assert "private user prompt" not in text + json.dumps(audit)
    assert json.loads(text)["exception_type"] == type(failure).__name__
    if expected is Outcome.TIMEOUT:
        assert client.request_status("one") is RequestStatus.UNKNOWN
    else:
        duplicate = client.call(request)
        assert dict(duplicate.diagnostics) == dict(response.diagnostics)


def test_local_processing_bug_is_not_misreported_as_provider_server_error(tmp_path, monkeypatch):
    client = HttpClient(api_key="unit-secret-key", raw_dir=tmp_path, opener=make_opener(openai_success()))

    def broken(**kwargs):
        raise ValueError("local processing bug unit-secret-key")

    monkeypatch.setattr(client, "_build_response", broken)
    response = client.call(ApiRequest(task_id="one", idempotency_key="one", prompt="test"))
    assert response.outcome is Outcome.INVALID_JSON and not response.retryable
    assert response.error_type == "client_processing_error"
    assert response.status_code == 200
    assert response.diagnostics["stage"] == "response_processing"
    assert response.diagnostics["exception_type"] == "ValueError"
    assert "unit-secret-key" not in json.dumps(response.to_audit_dict())


def test_actual_http_503_retains_its_real_status(tmp_path):
    client = HttpClient(api_key="unit-secret-key", raw_dir=tmp_path,
                        opener=make_opener({"error": "unavailable"}, status=503))
    response = client.call(ApiRequest(task_id="one", idempotency_key="one", prompt="test"))
    assert response.outcome is Outcome.SERVER_ERROR and response.status_code == 503
    assert response.error_type == "server_error"
    assert response.retryable
