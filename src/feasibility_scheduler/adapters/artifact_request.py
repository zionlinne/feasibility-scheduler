"""把 owned_files 交给 client，并在格式失败时仍走验收以便留下 Raw。

不改内核 Executor 的调度、Φ 或重试循环。格式修复发生在 client 内，
这里只负责带上交付范围，并在不可修复的 INVALID_JSON 上产出验收报告。
"""

from __future__ import annotations

from feasibility_scheduler.artifact_delivery import owned_files_from_payload
from feasibility_scheduler.fake_client import (
    DEFAULT_MODEL,
    FAKE_PROVIDER,
    ApiRequest,
    ApiResponse,
    Outcome,
)
from feasibility_scheduler.statemachine import TaskState


class ArtifactMetadataExecutorMixin:
    """供 legacy / rewrite 执行器复用，避免改 ``executor.py``。"""

    async def _send(self, task_id: str, key: str) -> ApiResponse:
        task = self._task_by_id[task_id]
        payload = task.payload if getattr(task, "payload", None) else {}
        owned = owned_files_from_payload(payload)
        request = ApiRequest(
            task_id=task_id,
            idempotency_key=key,
            prompt=self._prompt(task_id),
            model=getattr(self.client, "model", DEFAULT_MODEL),
            provider=getattr(self.client, "provider", FAKE_PROVIDER),
            metadata={
                "artifact_delivery": True,
                "owned_files": list(owned),
            },
        )
        if self.before_send is not None:
            await self.before_send(request)
        response = await self.client.acall(request)
        if self.after_send is not None:
            await self.after_send(request)
        self._record(
            "api_response_received" if response.ok else "api_request_failed",
            task_id,
            f"provider 返回 {response.outcome.value}",
            details=response.to_audit_dict(),
        )
        return response

    async def _handle_response(self, task_id: str, response: ApiResponse) -> bool:
        # 传输类失败仍走内核（重试 / 超时恢复）。格式失败先进入验收，报告并保留 Raw。
        if response.outcome is Outcome.INVALID_JSON:
            await self._step(
                task_id,
                TaskState.RESULT_RECEIVED,
                "api_request_failed",
                "格式不可用，保留 Raw 后进入验收："
                + (response.error_type or response.outcome.value),
                details={
                    "error_type": response.error_type,
                    "format_repair_used": bool(getattr(response, "format_repair_used", False)),
                },
            )
            await self._accept(task_id, response)
            return True
        return await super()._handle_response(task_id, response)
