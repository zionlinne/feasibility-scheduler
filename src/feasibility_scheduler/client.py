"""Scheduler API client 契约与工厂。

``FakeClient`` 与 ``HttpClient`` 共用同一 Protocol；默认仍走 Fake，保证现有
pytest 不触网。切换方式：

* 工厂：``create_client("fake")`` / ``create_client("http")``
* 环境变量：``FEASIBILITY_CLIENT=fake|http``（未设置时默认 ``fake``）
* 也可直接把已构造好的 client 注入 ``Executor(..., client=...)``
"""

from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

from .fake_client import (
    ApiRequest,
    ApiResponse,
    FakeClient,
    Outcome,
    RequestStatus,
    RequestValidationError,
)

__all__ = [
    "SchedulerClient",
    "create_client",
    "ApiRequest",
    "ApiResponse",
    "Outcome",
    "RequestStatus",
    "RequestValidationError",
    "FakeClient",
]


@runtime_checkable
class SchedulerClient(Protocol):
    """Executor 实际依赖的最小 client 接口。"""

    model: str
    provider: str

    def call(self, request: ApiRequest) -> ApiResponse:
        """同步调用。"""

    async def acall(self, request: ApiRequest) -> ApiResponse:
        """异步调用（Executor 主路径使用）。"""

    def request_status(self, idempotency_key: str) -> RequestStatus:
        """按幂等键报告 NOT_SENT / SENT / UNKNOWN。"""


def create_client(
    kind: str | None = None,
    **kwargs: Any,
) -> SchedulerClient:
    """按名称或环境变量构造 client。

    ``kind`` 优先；否则读 ``FEASIBILITY_CLIENT``；再否则 ``fake``。
    合法值：``fake``、``http``（别名 ``real``）。
    """
    selected = (kind if kind is not None else os.environ.get("FEASIBILITY_CLIENT", "fake"))
    selected = selected.strip().lower()
    if selected in {"", "fake"}:
        return FakeClient(**kwargs)
    if selected in {"http", "real"}:
        from .http_client import HttpClient

        return HttpClient(**kwargs)
    if selected == "native":
        from .native_client import NativeClient
        return NativeClient(**kwargs)
    raise ValueError(
        f"未知 FEASIBILITY_CLIENT={selected!r}，期望 fake|http"
    )
