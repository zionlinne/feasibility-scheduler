"""真实 88api 冒烟（默认跳过；需 FEASIBILITY_RUN_INTEGRATION=1 且有 key）。"""

from __future__ import annotations

import os

import pytest

from feasibility_scheduler.fake_client import ApiRequest, Outcome
from feasibility_scheduler.http_client import HttpClient, MissingApiKeyError, resolve_api_key

def _has_key() -> bool:
    try:
        return bool(resolve_api_key())
    except MissingApiKeyError:
        return False


def _integration_enabled() -> bool:
    return os.environ.get("FEASIBILITY_RUN_INTEGRATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _integration_enabled(),
        reason="默认跳过；设置 FEASIBILITY_RUN_INTEGRATION=1 后显式开启",
    ),
]


@pytest.mark.skipif(not _has_key(), reason="无可用 API Key，跳过真实请求")
def test_shortest_chat_completion():
    client = HttpClient(timeout_seconds=60)
    response = client.call(
        ApiRequest(
            task_id="smoke",
            idempotency_key="smoke-integration-1",
            prompt='Reply with exactly this JSON object and nothing else: {"ok": true}',
            model=os.environ.get("FEASIBILITY_MODEL", client.model),
            provider=client.provider,
        )
    )
    assert response.outcome is not Outcome.TIMEOUT
    assert client.request_status("smoke-integration-1").value in {"sent", "unknown"}
    audit = response.to_audit_dict()
    dumped = str(audit)
    # 密钥不得出现在审计字段
    assert "sk-unit" not in dumped
