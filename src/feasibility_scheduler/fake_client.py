"""可脚本化的 FakeClient：不发网络请求，所有行为来自测试脚本。

对应规格第 4.1、6.1 节。设计约束：

1. **无网络**：本模块只使用标准库的 ``json``/``hashlib``/``dataclasses``，
   不导入任何 HTTP 或 socket 库。测试可以替换 ``socket`` 为炸弹来证明这一点。
2. **脚本决定行为**：每次真实调用按顺序消费一个 :class:`Step`；脚本耗尽时抛
   :class:`FakeScriptExhausted`，绝不默认为成功。没有随机数，也没有真实等待。
3. **确定性**：请求 ID 由序号生成，时间来自逻辑时钟（固定起点，每次真实请求
   推进 1 秒），usage 与 JSON 序列化顺序固定。同一脚本、同一请求序列必然得到
   完全相同的结果。
4. **幂等键状态**：:meth:`FakeClient.request_status` 区分

   - ``SENT``：请求已发出并拿到 provider 响应（成功、限流、5xx、非法 JSON）；
   - ``NOT_SENT``：该键从未发出过请求；
   - ``UNKNOWN``：请求可能已到达 provider，但本地没有拿到可用响应（超时）。

   超时后默认拒绝用同一幂等键重发；只有显式开启
   ``allow_retry_after_unknown`` 才允许重试，并继续消费脚本。

5. **重复响应**：对已经成功发送的键，脚本必须显式给出
   :meth:`Step.duplicate`，此时返回首次响应的副本（同一 ``request_id``，标记
   ``is_duplicate``），不会再次“发出”请求，也不会增加尝试次数。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .artifact_delivery import (
    RepairText,
    allows_format_repair,
    is_artifact_request,
    owned_files_from_metadata,
    persist_raw_body,
    resolve_model_delivery,
)

FAKE_PROVIDER = "fake"
DEFAULT_MODEL = "fake-model-1"
_LOGICAL_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_LOGICAL_INTERVAL = timedelta(seconds=1)


class FakeClientError(Exception):
    """FakeClient 相关错误的基类。"""


class RequestValidationError(FakeClientError, ValueError):
    """请求字段不合法，请求没有被发出。"""


class FakeScriptExhausted(FakeClientError):
    """脚本已耗尽：测试脚本没有为这次调用安排行为。"""


class FakeScriptError(FakeClientError):
    """脚本本身与调用序列矛盾（例如首次请求就要求重复响应）。"""


class DuplicateRequestError(FakeClientError):
    """同一幂等键在没有 ``duplicate`` 脚本步骤时被再次调用。"""


class Outcome(str, Enum):
    """一次传输的结果类别。"""

    SUCCESS = "success"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    INVALID_JSON = "invalid_json"


class RequestStatus(str, Enum):
    """按幂等键报告的请求状态。"""

    NOT_SENT = "not_sent"
    SENT = "sent"
    UNKNOWN = "unknown"


_DEFAULT_STATUS_CODE = {
    Outcome.SUCCESS: 200,
    Outcome.TIMEOUT: 0,
    Outcome.RATE_LIMITED: 429,
    Outcome.SERVER_ERROR: 500,
    Outcome.INVALID_JSON: 200,
}

_DEFAULT_ERROR_TYPE = {
    Outcome.SUCCESS: None,
    Outcome.TIMEOUT: "timeout",
    Outcome.RATE_LIMITED: "rate_limited",
    Outcome.SERVER_ERROR: "server_error",
    Outcome.INVALID_JSON: "invalid_json",
}

_DEFAULT_LATENCY = {
    Outcome.SUCCESS: 0.25,
    Outcome.TIMEOUT: 30.0,
    Outcome.RATE_LIMITED: 0.10,
    Outcome.SERVER_ERROR: 0.20,
    Outcome.INVALID_JSON: 0.30,
}

#: 默认成功响应里的 usage，固定值以保证确定性。
_DEFAULT_USAGE = (("input_tokens", 8), ("output_tokens", 5))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise RequestValidationError("时间必须带时区，避免本地时间歧义")
    return value.astimezone(timezone.utc)


def _freeze_usage(value: Mapping[str, int]) -> Mapping[str, int]:
    frozen: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise RequestValidationError("usage 的键必须是非空字符串")
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise RequestValidationError(f"usage[{key!r}] 必须是非负整数")
        frozen[key] = item
    return MappingProxyType(frozen)


def _freeze_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({str(k): freeze(v) for k, v in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(v) for v in item)
        return item

    return freeze(value)  # type: ignore[return-value]


@dataclass(frozen=True)
class ApiRequest:
    """一次模型调用请求；``prompt`` 只用于计算输入哈希，不写入审计字段。"""

    task_id: str
    idempotency_key: str
    prompt: str
    model: str = DEFAULT_MODEL
    provider: str = FAKE_PROVIDER
    input_artifact_hash: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "idempotency_key", "prompt", "model", "provider"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RequestValidationError(f"{name} 不能为空")
        if self.input_artifact_hash is None:
            payload = json.dumps(
                {
                    "task_id": self.task_id,
                    "model": self.model,
                    "provider": self.provider,
                    "prompt": self.prompt,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            object.__setattr__(
                self,
                "input_artifact_hash",
                "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            )
        elif (
            not isinstance(self.input_artifact_hash, str)
            or not self.input_artifact_hash.strip()
        ):
            raise RequestValidationError("input_artifact_hash 必须是非空字符串")
        object.__setattr__(self, "metadata", _freeze_json(self.metadata))

    def audit_fields(self) -> dict[str, Any]:
        """规格 4.1 中可由请求方提供的字段，不含 prompt 正文。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "input_artifact_hash": self.input_artifact_hash,
        }


@dataclass(frozen=True)
class Step:
    """脚本中的一步行为。

    用工厂方法构造，避免测试直接拼装矛盾字段。``duplicate_response=True`` 表示
    这次调用应当返回首次响应的副本，而不是发出新请求。
    """

    outcome: Outcome = Outcome.SUCCESS
    body: str | None = '{"ok": true}'
    status_code: int | None = None
    retry_after_seconds: float | None = None
    error_type: str | None = None
    usage: tuple[tuple[str, int], ...] = _DEFAULT_USAGE
    latency_seconds: float | None = None
    duplicate_response: bool = False
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status_code is not None and not 0 <= self.status_code <= 599:
            raise RequestValidationError("status_code 必须在 0..599 之间")
        if self.retry_after_seconds is not None and self.retry_after_seconds <= 0:
            raise RequestValidationError("retry_after_seconds 必须大于 0")
        if self.latency_seconds is not None and self.latency_seconds < 0:
            raise RequestValidationError("latency_seconds 不能为负")
        for key, value in self.usage:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RequestValidationError(f"usage[{key!r}] 必须是非负整数")

    @property
    def effective_status_code(self) -> int:
        if self.status_code is not None:
            return self.status_code
        return _DEFAULT_STATUS_CODE[self.outcome]

    @property
    def effective_error_type(self) -> str | None:
        if self.error_type is not None:
            return self.error_type
        return _DEFAULT_ERROR_TYPE[self.outcome]

    @property
    def effective_latency(self) -> float:
        return (
            self.latency_seconds
            if self.latency_seconds is not None
            else _DEFAULT_LATENCY[self.outcome]
        )

    @classmethod
    def success(
        cls,
        body: str = '{"ok": true}',
        *,
        usage: Mapping[str, int] | None = None,
        status_code: int = 200,
        latency_seconds: float = 0.25,
        finish_reason: str | None = None,
    ) -> "Step":
        frozen_usage = tuple(
            sorted((usage if usage is not None else dict(_DEFAULT_USAGE)).items())
        )
        return cls(
            outcome=Outcome.SUCCESS,
            body=body,
            status_code=status_code,
            usage=frozen_usage,
            latency_seconds=latency_seconds,
            finish_reason=finish_reason,
        )

    @classmethod
    def timeout(cls, *, latency_seconds: float = 30.0) -> "Step":
        return cls(
            outcome=Outcome.TIMEOUT,
            body=None,
            status_code=0,
            latency_seconds=latency_seconds,
            usage=(),
        )

    @classmethod
    def rate_limited(
        cls,
        *,
        retry_after_seconds: float = 2.0,
        body: str = '{"error": "rate_limited"}',
    ) -> "Step":
        return cls(
            outcome=Outcome.RATE_LIMITED,
            body=body,
            retry_after_seconds=retry_after_seconds,
            usage=(),
        )

    @classmethod
    def server_error(
        cls,
        *,
        status_code: int = 500,
        body: str | None = None,
    ) -> "Step":
        if not 500 <= status_code <= 599:
            raise RequestValidationError("server_error 的 status_code 必须在 5xx")
        return cls(
            outcome=Outcome.SERVER_ERROR,
            body=body if body is not None else f'{{"error": {status_code}}}',
            status_code=status_code,
            usage=(),
        )

    @classmethod
    def invalid_json(
        cls,
        body: str = '{"broken": ',
        *,
        usage: Mapping[str, int] | None = None,
        finish_reason: str | None = None,
    ) -> "Step":
        frozen_usage = tuple(sorted(usage.items())) if usage is not None else ()
        return cls(
            outcome=Outcome.INVALID_JSON,
            body=body,
            usage=frozen_usage,
            finish_reason=finish_reason,
        )

    @classmethod
    def duplicate(cls) -> "Step":
        return cls(duplicate_response=True, body=None, usage=())


@dataclass(frozen=True)
class ApiResponse:
    """一次调用得到的响应；失败也以结果对象返回，便于统一记录。"""

    request_id: str
    idempotency_key: str
    task_id: str
    provider: str
    model: str
    outcome: Outcome
    status_code: int
    raw_body: str | None
    parsed_body: Mapping[str, Any] | None
    usage: Mapping[str, int]
    error_type: str | None
    retry_after_seconds: float | None
    is_duplicate: bool
    duplicate_of: str | None
    attempt: int
    started_at: datetime
    finished_at: datetime
    input_artifact_hash: str
    format_repair_used: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", _freeze_usage(self.usage))
        if self.parsed_body is not None:
            object.__setattr__(self, "parsed_body", _freeze_json(self.parsed_body))

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS

    @property
    def retryable(self) -> bool:
        """超时、限流、5xx 可重试。格式修复不是传输重试，不在这里循环。"""
        return self.outcome in {
            Outcome.TIMEOUT,
            Outcome.RATE_LIMITED,
            Outcome.SERVER_ERROR,
        }

    @property
    def format_repair_allowed(self) -> bool:
        """类别允许一次格式修复，且这一次还没用掉。成功响应不再修复。"""
        if self.format_repair_used or self.ok:
            return False
        return allows_format_repair(self.error_type)

    def to_audit_dict(self) -> dict[str, Any]:
        """规格 4.1 要求记录的字段；不含 prompt、API key 或原始正文。"""
        return {
            "request_id": self.request_id,
            "provider": self.provider,
            "model": self.model,
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "input_artifact_hash": self.input_artifact_hash,
            "started_at": self.started_at.isoformat(timespec="microseconds"),
            "finished_at": self.finished_at.isoformat(timespec="microseconds"),
            "status_code": self.status_code,
            "outcome": self.outcome.value,
            "error_type": self.error_type,
            "retry_after_seconds": self.retry_after_seconds,
            "usage": dict(self.usage),
            "parse_status": (
                "parsed"
                if self.parsed_body is not None
                else ("not_attempted" if self.outcome is Outcome.TIMEOUT else "failed")
            ),
            "attempt": self.attempt,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "format_repair_used": self.format_repair_used,
        }


@dataclass
class _LedgerEntry:
    status: RequestStatus
    attempts: int
    last_response: ApiResponse | None
    request_fingerprint: tuple[str, str, str] | None = None
    duplicates: int = 0


class FakeClient:
    """按脚本返回确定性响应的客户端，可直接替换真实 API client。"""

    def __init__(
        self,
        script: Iterable[Step] = (),
        *,
        model: str = DEFAULT_MODEL,
        provider: str = FAKE_PROVIDER,
        clock: Callable[[], datetime] | None = None,
        allow_retry_after_unknown: bool = False,
        raw_dir: str | Path | None = None,
    ) -> None:
        if not model.strip() or not provider.strip():
            raise RequestValidationError("model/provider 不能为空")
        self.model = model
        self.provider = provider
        self._initial_script = tuple(script)
        self._script = self._initial_script
        self._cursor = 0
        self._clock = clock
        self._allow_retry_after_unknown = allow_retry_after_unknown
        self.raw_dir = raw_dir
        self.received_raw: list[dict[str, Any]] = []
        self.repair_prompts: list[str] = []
        self._ledger: dict[str, _LedgerEntry] = {}
        self._responses: list[ApiResponse] = []
        self._request_serial = 0
        self._call_index = 0

    # ------------------------------------------------------------- 调用

    def call(self, request: ApiRequest) -> ApiResponse:
        """同步调用；不睡眠、不联网、不使用随机数。"""
        if not isinstance(request, ApiRequest):
            raise RequestValidationError("request 必须是 ApiRequest")

        entry = self._ledger.get(request.idempotency_key)
        fingerprint = (
            request.task_id,
            request.input_artifact_hash or "",
            request.model,
        )
        if entry is not None:
            if entry.request_fingerprint != fingerprint:
                raise DuplicateRequestError(
                    f"幂等键 {request.idempotency_key!r} 已绑定到另一个请求"
                    "（task、输入哈希或模型不同），拒绝复用"
                )
            retrying_unknown = (
                entry.status is RequestStatus.UNKNOWN
                and self._allow_retry_after_unknown
            )
            if not retrying_unknown:
                return self._replay_duplicate(request, entry)
        else:
            entry = _LedgerEntry(
                status=RequestStatus.NOT_SENT,
                attempts=0,
                last_response=None,
            )

        step = self._peek_step()
        if step.duplicate_response:
            raise FakeScriptError(
                "脚本要求 duplicate，但该幂等键没有可重复返回的首次响应"
            )
        self._cursor += 1

        self._request_serial += 1
        request_id = f"fake-{self._request_serial:06d}"
        started_at, finished_at = self._timestamps(step.effective_latency)
        parsed_body, outcome, error_type, format_repair_used = self._interpret_step(
            request, step
        )

        entry.attempts += 1
        response = ApiResponse(
            request_id=request_id,
            idempotency_key=request.idempotency_key,
            task_id=request.task_id,
            provider=self.provider,
            model=self.model,
            outcome=outcome,
            status_code=step.effective_status_code,
            raw_body=step.body,
            parsed_body=parsed_body,
            usage=dict(step.usage),
            error_type=error_type,
            retry_after_seconds=step.retry_after_seconds,
            is_duplicate=False,
            duplicate_of=None,
            attempt=entry.attempts,
            started_at=started_at,
            finished_at=finished_at,
            input_artifact_hash=request.input_artifact_hash or "",
            format_repair_used=format_repair_used,
        )
        entry.status = self._status_for(response)
        entry.last_response = response
        entry.request_fingerprint = fingerprint
        self._ledger[request.idempotency_key] = entry
        self._responses.append(response)
        return response

    async def acall(self, request: ApiRequest) -> ApiResponse:
        """异步包装，供 asyncio 调度器按真实 client 的接口调用。"""
        await asyncio.sleep(0)
        return self.call(request)

    # ------------------------------------------------------------- 查询

    def request_status(self, idempotency_key: str) -> RequestStatus:
        entry = self._ledger.get(idempotency_key)
        return entry.status if entry is not None else RequestStatus.NOT_SENT

    def attempts(self, idempotency_key: str) -> int:
        entry = self._ledger.get(idempotency_key)
        return entry.attempts if entry is not None else 0

    def duplicate_count(self, idempotency_key: str) -> int:
        entry = self._ledger.get(idempotency_key)
        return entry.duplicates if entry is not None else 0

    @property
    def responses(self) -> tuple[ApiResponse, ...]:
        return tuple(self._responses)

    @property
    def script_remaining(self) -> int:
        return len(self._script) - self._cursor

    def reset(self) -> None:
        """回到初始状态，用于验证同一输入得到同一结果。"""
        self._script = self._initial_script
        self._cursor = 0
        self._ledger.clear()
        self._responses.clear()
        self.received_raw.clear()
        self.repair_prompts.clear()
        self._request_serial = 0
        self._call_index = 0

    # ------------------------------------------------------------- 内部

    def _peek_step(self) -> Step:
        if self._cursor >= len(self._script):
            raise FakeScriptExhausted(
                f"脚本只有 {len(self._script)} 步，第 {self._cursor + 1} 次调用没有安排行为"
            )
        return self._script[self._cursor]

    def _replay_duplicate(
        self, request: ApiRequest, entry: _LedgerEntry
    ) -> ApiResponse:
        step = self._peek_step()
        if not step.duplicate_response:
            raise DuplicateRequestError(
                f"幂等键 {request.idempotency_key!r} 已发出过请求"
                f"（状态 {entry.status.value}），脚本下一步不是 duplicate"
            )
        first = entry.last_response
        if first is None or first.outcome is not Outcome.SUCCESS:
            raise FakeScriptError(
                "只有成功的首次响应才能模拟重复投递"
            )
        self._cursor += 1
        entry.duplicates += 1
        duplicate = ApiResponse(
            request_id=first.request_id,
            idempotency_key=first.idempotency_key,
            task_id=first.task_id,
            provider=first.provider,
            model=first.model,
            outcome=first.outcome,
            status_code=first.status_code,
            raw_body=first.raw_body,
            parsed_body=dict(first.parsed_body) if first.parsed_body is not None else None,
            usage=dict(first.usage),
            error_type=first.error_type,
            retry_after_seconds=first.retry_after_seconds,
            is_duplicate=True,
            duplicate_of=first.request_id,
            attempt=first.attempt,
            started_at=first.started_at,
            finished_at=first.finished_at,
            input_artifact_hash=first.input_artifact_hash,
            format_repair_used=first.format_repair_used,
        )
        self._responses.append(duplicate)
        return duplicate

    def _timestamps(self, latency_seconds: float) -> tuple[datetime, datetime]:
        if self._clock is None:
            started_at = _LOGICAL_EPOCH + self._call_index * _LOGICAL_INTERVAL
        else:
            started_at = _utc(self._clock())
        self._call_index += 1
        return started_at, started_at + timedelta(seconds=latency_seconds)

    def _interpret_step(
        self, request: ApiRequest, step: Step
    ) -> tuple[Mapping[str, Any] | None, Outcome, str | None, bool]:
        """传输错误保持原样；成功/非法 JSON 走与 HTTP client 相同的交付分类。

        格式修复在同一次 ``call`` 内最多再消费一个脚本步，不增加执行器重试。
        原始失败正文留在 ``raw_body``，不因修复成功而被覆盖。
        """
        if step.outcome not in {Outcome.SUCCESS, Outcome.INVALID_JSON}:
            return None, step.outcome, step.effective_error_type, False

        artifact_mode = is_artifact_request(request.metadata)
        owned = owned_files_from_metadata(request.metadata) if artifact_mode else ()
        if isinstance(step.body, str):
            persist_raw_body(
                self.received_raw,
                raw_dir=self.raw_dir,
                task_id=request.task_id,
                idempotency_key=request.idempotency_key,
                body=step.body,
                phase="before_parse",
            )

        def fetch_repair(prompt: str) -> RepairText | None:
            self.repair_prompts.append(prompt)
            if self._cursor >= len(self._script):
                return None
            nxt = self._script[self._cursor]
            if nxt.duplicate_response or nxt.outcome not in {
                Outcome.SUCCESS,
                Outcome.INVALID_JSON,
            }:
                return None
            self._cursor += 1
            text = nxt.body if isinstance(nxt.body, str) else ""
            persist_raw_body(
                self.received_raw,
                raw_dir=self.raw_dir,
                task_id=request.task_id,
                idempotency_key=request.idempotency_key + ":format-repair",
                body=text,
                phase="repair_before_parse",
            )
            return RepairText(
                content=text,
                usage=dict(nxt.usage),
                finish_reason=nxt.finish_reason,
            )

        final = resolve_model_delivery(
            step.body,
            usage=dict(step.usage),
            finish_reason=step.finish_reason,
            owned_files=owned,
            artifact_mode=artifact_mode,
            fetch_repair=fetch_repair,
        )
        outcome = Outcome.SUCCESS if final.outcome == "success" else Outcome.INVALID_JSON
        return final.parsed_body, outcome, final.error_type, final.format_repair_used

    @staticmethod
    def _parse_body(step: Step) -> tuple[Mapping[str, Any] | None, bool]:
        if step.outcome is not Outcome.SUCCESS:
            return None, False
        if step.body is None:
            return None, True
        try:
            parsed = json.loads(step.body)
        except json.JSONDecodeError:
            return None, True
        if not isinstance(parsed, dict):
            return None, True
        return parsed, False

    @staticmethod
    def _status_for(response: ApiResponse) -> RequestStatus:
        if response.outcome is Outcome.TIMEOUT:
            return RequestStatus.UNKNOWN
        return RequestStatus.SENT
