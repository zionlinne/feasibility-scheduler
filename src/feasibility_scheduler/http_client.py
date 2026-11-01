"""OpenAI 兼容的真实 HTTP client（对接 88api 等）。

- 仅用标准库 ``urllib`` / ``http.client``，无第三方 HTTP 依赖。
- API Key：``FEASIBILITY_API_KEY`` / ``OPENAI_API_KEY``，否则尝试 DPAPI 文件。
- **绝不**把密钥写入源码、日志或异常消息明文。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin

from .artifact_delivery import (
    OUTPUT_TOKEN_HARD_LIMIT,
    RepairText,
    is_artifact_request,
    is_true_output_truncation,
    owned_files_from_metadata,
    parse_json_object,
    persist_raw_body,
    resolve_model_delivery,
)
from .fake_client import (
    ApiRequest,
    ApiResponse,
    Outcome,
    RequestStatus,
    RequestValidationError,
)

DEFAULT_BASE_URL = "https://api.88api.ai/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_PROVIDER = "88api"
DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_DPAPI_KEY_FILE = Path(r"D:\airoom-studio\relay-probe\88api\key.dpapi")


class HttpClientError(Exception):
    """HttpClient 相关错误。"""


class MissingApiKeyError(HttpClientError):
    """找不到可用的 API Key。"""


@dataclass
class _LedgerEntry:
    status: RequestStatus
    attempts: int
    last_response: ApiResponse | None
    request_fingerprint: tuple[str, str, str] | None = None
    duplicates: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_api_key(
    api_key: str | None = None,
    *,
    key_file: str | Path | None = None,
) -> str:
    """解析 API Key：显式参数 → 环境变量 → DPAPI 文件。

    返回明文 key；调用方负责不要打印它。
    """
    if api_key is not None and str(api_key).strip():
        return str(api_key).strip()
    for env_name in ("FEASIBILITY_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()
    path = Path(key_file) if key_file is not None else None
    if path is None:
        env_path = os.environ.get("FEASIBILITY_API_KEY_FILE")
        path = Path(env_path) if env_path else _DEFAULT_DPAPI_KEY_FILE
    if path.is_file():
        return _decrypt_dpapi_key(path)
    raise MissingApiKeyError(
        "未找到 API Key：请设置 FEASIBILITY_API_KEY / OPENAI_API_KEY，"
        "或提供可读的 DPAPI key 文件"
    )


def _decrypt_dpapi_key(path: Path) -> str:
    """用当前用户 DPAPI（ConvertTo-SecureString）解密 key 文件。"""
    # 与 airoom-studio/relay-probe/88api/lib88.js 同路径解密方式。
    literal = str(path).replace("'", "''")
    command = (
        f"$s = Get-Content -LiteralPath '{literal}' | ConvertTo-SecureString; "
        "$b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
        "try { [Console]::Out.Write("
        "[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)) } "
        "finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MissingApiKeyError("无法通过 PowerShell 解密 DPAPI key 文件") from exc
    key = (completed.stdout or "").strip()
    if completed.returncode != 0 or not key:
        raise MissingApiKeyError("DPAPI key 文件解密失败或结果为空")
    return key


def _scrub(text: str, secrets: list[str]) -> str:
    scrubbed = text
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "***")
    # 兜底：截断疑似 sk- 形态密钥
    import re

    return re.sub(r"(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+", r"\1***", scrubbed)


class HttpClient:
    """OpenAI 兼容 Chat Completions client，行为对齐 FakeClient 契约。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        provider: str = DEFAULT_PROVIDER,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        key_file: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        allow_retry_after_unknown: bool = False,
        opener: Callable[..., Any] | None = None,
        raw_dir: str | Path | None = None,
    ) -> None:
        if not model.strip() or not provider.strip():
            raise RequestValidationError("model/provider 不能为空")
        if timeout_seconds <= 0:
            raise RequestValidationError("timeout_seconds 必须大于 0")
        self.model = model
        self.provider = provider
        self.base_url = (base_url or os.environ.get("FEASIBILITY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._clock = clock or _utc_now
        self._allow_retry_after_unknown = allow_retry_after_unknown
        self._opener = opener or urllib.request.urlopen
        self._api_key = resolve_api_key(api_key, key_file=key_file)
        self._secrets = [self._api_key]
        self.raw_dir = Path(raw_dir) if raw_dir is not None else None
        self.received_raw: list[dict[str, Any]] = []
        self.repair_prompts: list[str] = []
        self._ledger: dict[str, _LedgerEntry] = {}
        self._responses: list[ApiResponse] = []
        self._request_serial = 0

    # ------------------------------------------------------------- 调用

    def call(self, request: ApiRequest) -> ApiResponse:
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
                raise RequestValidationError(
                    f"幂等键 {request.idempotency_key!r} 已绑定到另一个请求"
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

        self._request_serial += 1
        request_id = f"http-{self._request_serial:06d}"
        started_at = self._clock()
        if started_at.tzinfo is None:
            raise RequestValidationError("时间必须带时区")

        try:
            status_code, raw_text, headers = self._post_chat(request)
            finished_at = self._clock()
            response = self._build_response(
                request=request,
                request_id=request_id,
                status_code=status_code,
                raw_text=raw_text,
                headers=headers,
                attempt=entry.attempts + 1,
                started_at=started_at,
                finished_at=finished_at,
            )
        except TimeoutError:
            finished_at = self._clock()
            response = ApiResponse(
                request_id=request_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                provider=self.provider,
                model=request.model or self.model,
                outcome=Outcome.TIMEOUT,
                status_code=0,
                raw_body=None,
                parsed_body=None,
                usage={},
                error_type="timeout",
                retry_after_seconds=None,
                is_duplicate=False,
                duplicate_of=None,
                attempt=entry.attempts + 1,
                started_at=started_at,
                finished_at=finished_at,
                input_artifact_hash=request.input_artifact_hash or "",
            )
        except Exception as exc:  # 网络层未知错误：当作可重试 server_error，不泄露 key
            finished_at = self._clock()
            msg = _scrub(f"{type(exc).__name__}: {exc}", self._secrets)
            response = ApiResponse(
                request_id=request_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                provider=self.provider,
                model=request.model or self.model,
                outcome=Outcome.SERVER_ERROR,
                status_code=0,
                raw_body=msg,
                parsed_body=None,
                usage={},
                error_type="server_error",
                retry_after_seconds=None,
                is_duplicate=False,
                duplicate_of=None,
                attempt=entry.attempts + 1,
                started_at=started_at,
                finished_at=finished_at,
                input_artifact_hash=request.input_artifact_hash or "",
            )

        # 用服务端 id 覆盖本地序号（若有）
        if response.ok and response.parsed_body is not None:
            # request_id already set; optionally enrich from OpenAI id in raw HTTP
            pass

        entry.attempts += 1
        entry.status = (
            RequestStatus.UNKNOWN
            if response.outcome is Outcome.TIMEOUT
            else RequestStatus.SENT
        )
        entry.last_response = response
        entry.request_fingerprint = fingerprint
        self._ledger[request.idempotency_key] = entry
        self._responses.append(response)
        return response

    async def acall(self, request: ApiRequest) -> ApiResponse:
        return await asyncio.to_thread(self.call, request)

    # ------------------------------------------------------------- 查询

    def request_status(self, idempotency_key: str) -> RequestStatus:
        entry = self._ledger.get(idempotency_key)
        return entry.status if entry is not None else RequestStatus.NOT_SENT

    def attempts(self, idempotency_key: str) -> int:
        entry = self._ledger.get(idempotency_key)
        return entry.attempts if entry is not None else 0

    def lookup_response(self, idempotency_key: str) -> ApiResponse | None:
        entry = self._ledger.get(idempotency_key)
        if entry is None:
            return None
        return entry.last_response

    @property
    def responses(self) -> tuple[ApiResponse, ...]:
        return tuple(self._responses)

    # ------------------------------------------------------------- 内部

    def _chat_url(self) -> str:
        return urljoin(self.base_url + "/", "chat/completions")

    def _resolve_model(self, request: ApiRequest) -> str:
        # Executor 会写入 client.model；直接构造 ApiRequest 时也可能带默认 fake-model-1
        if request.model and request.model != "fake-model-1":
            return request.model
        return self.model

    def _post_chat(self, request: ApiRequest) -> tuple[int, str, Mapping[str, str]]:
        model = self._resolve_model(request)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": request.idempotency_key,
        }
        req = urllib.request.Request(
            self._chat_url(),
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with self._opener(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = int(getattr(resp, "status", 200))
                hdrs = {k.lower(): v for k, v in getattr(resp, "headers", {}).items()}
                return status, raw, hdrs
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            return int(exc.code), raw, hdrs
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise TimeoutError(str(reason)) from exc
            raise

    def _build_response(
        self,
        *,
        request: ApiRequest,
        request_id: str,
        status_code: int,
        raw_text: str,
        headers: Mapping[str, str],
        attempt: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> ApiResponse:
        model = self._resolve_model(request)

        retry_after = self._parse_retry_after(headers)
        scrubbed_raw = _scrub(raw_text, self._secrets)
        # 原始正文一到达就落盘，先于任何 json.loads。解析失败也保留这份 Raw。
        self._remember_raw(request, scrubbed_raw, phase="before_parse")

        if status_code == 429:
            return ApiResponse(
                request_id=request_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                provider=self.provider,
                model=model,
                outcome=Outcome.RATE_LIMITED,
                status_code=status_code,
                raw_body=scrubbed_raw,
                parsed_body=None,
                usage={},
                error_type="rate_limited",
                retry_after_seconds=retry_after or 2.0,
                is_duplicate=False,
                duplicate_of=None,
                attempt=attempt,
                started_at=started_at,
                finished_at=finished_at,
                input_artifact_hash=request.input_artifact_hash or "",
            )
        if 500 <= status_code <= 599:
            return ApiResponse(
                request_id=request_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                provider=self.provider,
                model=model,
                outcome=Outcome.SERVER_ERROR,
                status_code=status_code,
                raw_body=scrubbed_raw,
                parsed_body=None,
                usage={},
                error_type="server_error",
                retry_after_seconds=None,
                is_duplicate=False,
                duplicate_of=None,
                attempt=attempt,
                started_at=started_at,
                finished_at=finished_at,
                input_artifact_hash=request.input_artifact_hash or "",
            )
        if status_code < 200 or status_code >= 300:
            # 其它 4xx：当作不可重试的解析/请求失败
            return ApiResponse(
                request_id=request_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                provider=self.provider,
                model=model,
                outcome=Outcome.INVALID_JSON,
                status_code=status_code,
                raw_body=scrubbed_raw,
                parsed_body=None,
                usage={},
                error_type="invalid_json",
                retry_after_seconds=None,
                is_duplicate=False,
                duplicate_of=None,
                attempt=attempt,
                started_at=started_at,
                finished_at=finished_at,
                input_artifact_hash=request.input_artifact_hash or "",
            )

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return self._delivery_from_text(
                request=request,
                request_id=request_id,
                model=model,
                status_code=status_code,
                text=scrubbed_raw,
                raw_body=scrubbed_raw,
                usage={},
                finish_reason=None,
                attempt=attempt,
                started_at=started_at,
                finished_at=finished_at,
            )

        if not isinstance(payload, dict):
            return self._delivery_from_text(
                request=request,
                request_id=request_id,
                model=model,
                status_code=status_code,
                text=scrubbed_raw,
                raw_body=scrubbed_raw,
                usage={},
                finish_reason=None,
                attempt=attempt,
                started_at=started_at,
                finished_at=finished_at,
            )

        server_id = payload.get("id")
        if isinstance(server_id, str) and server_id.strip():
            request_id = server_id.strip()

        usage = self._extract_usage(payload)
        content = self._extract_content(payload)
        return self._delivery_from_text(
            request=request,
            request_id=request_id,
            model=model,
            status_code=status_code,
            text=content,
            raw_body=content if content is not None else scrubbed_raw,
            usage=usage,
            finish_reason=self._extract_finish_reason(payload),
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _remember_raw(self, request: ApiRequest, body: str, *, phase: str) -> None:
        persist_raw_body(
            self.received_raw,
            raw_dir=self.raw_dir,
            task_id=request.task_id,
            idempotency_key=request.idempotency_key,
            body=body,
            phase=phase,
        )

    def _fetch_format_repair(self, request: ApiRequest, prompt: str) -> RepairText | None:
        """恰好一次格式修复请求。取回正文前先落 Raw；失败则停止，不再请求。"""
        self.repair_prompts.append(prompt)
        repair_request = ApiRequest(
            task_id=request.task_id,
            idempotency_key=request.idempotency_key + ":format-repair",
            prompt=prompt,
            model=request.model,
            provider=request.provider,
            input_artifact_hash=request.input_artifact_hash,
            metadata=dict(request.metadata),
        )
        try:
            status_code, raw_text, _headers = self._post_chat(repair_request)
        except Exception:
            return None
        scrubbed = _scrub(raw_text, self._secrets)
        self._remember_raw(repair_request, scrubbed, phase="repair_before_parse")
        if status_code < 200 or status_code >= 300:
            return None
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return RepairText(content=scrubbed, usage={}, finish_reason=None)
        if not isinstance(payload, dict):
            return RepairText(content=scrubbed, usage={}, finish_reason=None)
        return RepairText(
            content=self._extract_content(payload),
            usage=self._extract_usage(payload),
            finish_reason=self._extract_finish_reason(payload),
        )

    def _delivery_from_text(
        self,
        *,
        request: ApiRequest,
        request_id: str,
        model: str,
        status_code: int,
        text: str | None,
        raw_body: str | None,
        usage: Mapping[str, int],
        finish_reason: str | None,
        attempt: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> ApiResponse:
        artifact_mode = is_artifact_request(request.metadata)
        owned = owned_files_from_metadata(request.metadata) if artifact_mode else ()
        final = resolve_model_delivery(
            text,
            usage=usage,
            finish_reason=finish_reason,
            owned_files=owned,
            artifact_mode=artifact_mode,
            fetch_repair=lambda prompt: self._fetch_format_repair(request, prompt),
        )
        outcome = Outcome.SUCCESS if final.outcome == "success" else Outcome.INVALID_JSON
        return ApiResponse(
            request_id=request_id,
            idempotency_key=request.idempotency_key,
            task_id=request.task_id,
            provider=self.provider,
            model=model,
            outcome=outcome,
            status_code=status_code,
            raw_body=raw_body,
            parsed_body=final.parsed_body,
            usage=dict(usage),
            error_type=final.error_type,
            retry_after_seconds=None,
            is_duplicate=False,
            duplicate_of=None,
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
            input_artifact_hash=request.input_artifact_hash or "",
            format_repair_used=final.format_repair_used,
        )

    @staticmethod
    def _extract_usage(payload: Mapping[str, Any]) -> dict[str, int]:
        usage = payload.get("usage") or {}
        if not isinstance(usage, Mapping):
            return {}
        out: dict[str, int] = {}
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0:
            out["input_tokens"] = prompt
        if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0:
            out["output_tokens"] = completion
        return out

    @staticmethod
    def _extract_content(payload: Mapping[str, Any]) -> str | None:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, Mapping):
            return None
        message = first.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                return content
        text = first.get("text")
        if isinstance(text, str):
            return text
        return None

    @staticmethod
    def _extract_finish_reason(payload: Mapping[str, Any]) -> str | None:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, Mapping):
            return None
        reason = first.get("finish_reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip().lower()
        return None

    @staticmethod
    def _looks_truncated(
        *,
        usage: Mapping[str, int],
        finish_reason: str | None,
        content: str | None,
    ) -> bool:
        """基于现有响应字段判断是否输出触顶/截断（不发新请求）。"""
        return is_true_output_truncation(usage if isinstance(usage, Mapping) else None, finish_reason)

    @staticmethod
    def _parse_artifact(content: str | None) -> tuple[Mapping[str, Any] | None, bool]:
        """把模型输出解析成验收用的 JSON 对象。

        - 内容本身是 JSON object → 用之
        - 内容缺失但 HTTP 已是 object（无 choices）→ 失败
        - 非 object JSON / 非法 JSON → 解析失败（具体类别由交付分类决定）
        - 触顶截断由 usage/finish_reason 标 output_truncated，不靠括号是否闭合
        """
        parsed, ok = parse_json_object(content)
        return parsed, not ok

    @staticmethod
    def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
        value = headers.get("retry-after")
        if value is None:
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    def _replay_duplicate(
        self, request: ApiRequest, entry: _LedgerEntry
    ) -> ApiResponse:
        first = entry.last_response
        if first is None:
            raise RequestValidationError(
                f"幂等键 {request.idempotency_key!r} 无首次响应可重复"
            )
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
