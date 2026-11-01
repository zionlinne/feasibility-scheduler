#!/usr/bin/env python3
"""最短真实请求冒烟：有 key 才请求，否则退出码 0 并说明 skip。"""

from __future__ import annotations

import json
import os
import sys

# 允许直接脚本运行
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from feasibility_scheduler.fake_client import ApiRequest
from feasibility_scheduler.http_client import HttpClient, MissingApiKeyError, resolve_api_key


def main() -> int:
    try:
        resolve_api_key()
    except MissingApiKeyError as exc:
        print(f"SKIP: {exc}")
        return 0

    model = os.environ.get("FEASIBILITY_MODEL", "deepseek-v4.1-flash")
    client = HttpClient(model=model, timeout_seconds=60)
    response = client.call(
        ApiRequest(
            task_id="smoke",
            idempotency_key="smoke-script-1",
            prompt='Reply with exactly this JSON object and nothing else: {"ok": true}',
            model=model,
            provider=client.provider,
        )
    )
    # 不打印 raw_body 全文，避免意外带出敏感信息；只打审计字段
    print(json.dumps(response.to_audit_dict(), ensure_ascii=False, indent=2))
    print("outcome=", response.outcome.value, "ok=", response.ok)
    return 0 if response.outcome.value != "timeout" else 2


if __name__ == "__main__":
    raise SystemExit(main())
