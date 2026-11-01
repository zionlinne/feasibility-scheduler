"""Artifact delivery classification shared by FakeClient and HttpClient.

JSON 对象解析与 ACS ``extract_files`` 两条路径都保留。真正的输出截断
（finish_reason / completion token 触顶）与「JSON 失败且抽不出文件」分开：

- ``invalid_format_no_artifact``：允许且只允许一次格式修复
- ``partial_artifact``：只补缺的 ``owned_files``，不覆盖已完成文件
- ``output_truncated``：真实截断，不再当作普通 ``invalid_json``，也不循环重试
- ``invalid_json``：协议层 4xx 等，不走格式修复

格式修复由本模块统一决定；两个 client 只负责取回下一次正文。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# 与历史 HTTP client 常量对齐：completion token 触顶视为真实截断。
OUTPUT_TOKEN_HARD_LIMIT = 65536
_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens", "max_length"})

ERROR_TIMEOUT = "timeout"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_SERVER = "server_error"
ERROR_INVALID_JSON = "invalid_json"
ERROR_OUTPUT_TRUNCATED = "output_truncated"
ERROR_INVALID_FORMAT_NO_ARTIFACT = "invalid_format_no_artifact"
ERROR_PARTIAL_ARTIFACT = "partial_artifact"

RECOVERY_RETRY_TRANSPORT = "retry_transport"
RECOVERY_FORMAT_REPAIR_ONCE = "format_repair_once"
RECOVERY_NONE = "none"

#: 两个 client 共用的错误类别 → 恢复规则。格式修复只出现在这一张表里。
RECOVERY_BY_ERROR: dict[str | None, str] = {
    ERROR_TIMEOUT: RECOVERY_RETRY_TRANSPORT,
    ERROR_RATE_LIMITED: RECOVERY_RETRY_TRANSPORT,
    ERROR_SERVER: RECOVERY_RETRY_TRANSPORT,
    ERROR_INVALID_JSON: RECOVERY_NONE,
    ERROR_OUTPUT_TRUNCATED: RECOVERY_NONE,
    ERROR_INVALID_FORMAT_NO_ARTIFACT: RECOVERY_FORMAT_REPAIR_ONCE,
    ERROR_PARTIAL_ARTIFACT: RECOVERY_FORMAT_REPAIR_ONCE,
    None: RECOVERY_NONE,
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def recovery_for(error_type: str | None) -> str:
    """返回该错误类别的恢复规则。未知类别不重试、不格式修复。"""
    return RECOVERY_BY_ERROR.get(error_type, RECOVERY_NONE)


def allows_format_repair(error_type: str | None) -> bool:
    """该错误类别是否允许恰好一次格式修复（执行前还要有 owned_files 范围）。"""
    return recovery_for(error_type) == RECOVERY_FORMAT_REPAIR_ONCE


def is_true_output_truncation(
    usage: Mapping[str, int] | None,
    finish_reason: str | None,
) -> bool:
    """真实截断：finish_reason 触顶，或 completion/output token 达到硬上限。

    仅仅 JSON 没闭合、或正文不是 JSON，不算截断，以免把格式错误标成不可修复的截断。
    """
    if isinstance(finish_reason, str) and finish_reason.strip().lower() in _TRUNCATION_FINISH_REASONS:
        return True
    if not isinstance(usage, Mapping):
        return False
    out_tokens = usage.get("output_tokens")
    return (
        isinstance(out_tokens, int)
        and not isinstance(out_tokens, bool)
        and out_tokens >= OUTPUT_TOKEN_HARD_LIMIT
    )


def parse_json_object(content: str | None) -> tuple[dict[str, Any] | None, bool]:
    """把模型正文解析成 JSON object。容忍一层 ```json 围栏。失败返回 (None, False)。"""
    if content is None:
        return None, False
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, False
    if not isinstance(parsed, dict):
        return None, False
    return parsed, True


def normalize_paths(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        norm = item.replace("\\", "/").strip().lstrip("./")
        if norm and norm not in out:
            out.append(norm)
    return tuple(out)


def is_artifact_request(metadata: Mapping[str, Any] | None) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    if metadata.get("artifact_delivery") is True:
        return True
    return "owned_files" in metadata


def owned_files_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(metadata, Mapping):
        return ()
    return normalize_paths(metadata.get("owned_files"))


def owned_files_from_payload(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    return normalize_paths(payload.get("owned_files"))


def files_from_json_body(body: Mapping[str, Any]) -> dict[str, str]:
    """从已解析的交付 JSON 取出 path→content。不覆盖调用方已有文件。"""
    files: dict[str, str] = {}
    delivery = body.get("delivery")
    if delivery == "file":
        file_obj = body.get("file")
        if isinstance(file_obj, Mapping):
            path = file_obj.get("path")
            content = file_obj.get("content")
            if isinstance(path, str) and isinstance(content, str) and content.strip():
                files[path.replace("\\", "/").lstrip("./")] = content
        if files:
            return files
    raw_files = body.get("files")
    if isinstance(raw_files, Mapping):
        for path, content in raw_files.items():
            if isinstance(path, str) and isinstance(content, str) and content.strip():
                files[path.replace("\\", "/").lstrip("./")] = content
    if not files:
        artifacts = body.get("artifacts")
        if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
            for item in artifacts:
                if not isinstance(item, Mapping):
                    continue
                path = item.get("path")
                content = item.get("content")
                if isinstance(path, str) and isinstance(content, str) and content.strip():
                    files.setdefault(path.replace("\\", "/").lstrip("./"), content)
    return files


def merge_files(
    existing: Mapping[str, str],
    incoming: Mapping[str, str],
) -> dict[str, str]:
    """合并交付。已有路径保持原内容，后到的正文不能覆盖已完成文件。"""
    merged = {path: content for path, content in existing.items() if isinstance(content, str)}
    for path, content in incoming.items():
        if path in merged:
            continue
        if isinstance(path, str) and isinstance(content, str) and content.strip():
            merged[path] = content
    return merged


def persist_raw_body(
    store: list[dict[str, Any]],
    *,
    raw_dir: str | Path | None,
    task_id: str,
    idempotency_key: str,
    body: str,
    phase: str,
) -> dict[str, Any]:
    """在任何 JSON 解析之前落下 Raw。解析失败也必须留下这份正文。"""
    record: dict[str, Any] = {
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "phase": phase,
        "body": body,
    }
    store.append(record)
    if raw_dir is None:
        return record
    directory = Path(raw_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = _SAFE_NAME.sub("_", f"{task_id}__{idempotency_key}__{phase}")[:180]
    path = directory / f"{stem}.raw.txt"
    path.write_text(body, encoding="utf-8")
    record["path"] = str(path)
    return record


def build_format_repair_prompt(
    owned_files: Sequence[str],
    missing_files: Sequence[str],
) -> str:
    """一次格式修复提示：只要求缺的 owned_files，且必须是 ACS 认得的 File + fence。"""
    owned = list(normalize_paths(owned_files))
    missing = list(normalize_paths(missing_files)) or list(owned)
    lines = [
        "FORMAT REPAIR (single attempt; do not expect another retry).",
        "The previous response was not usable as an artifact delivery.",
        "Re-emit ONLY the missing owned files listed below.",
        "Use ACS-recognizable File path + fence format strictly.",
        "Do not wrap the answer in JSON. Do not add prose before or after the fences.",
        "Do not resend files that were already delivered, and do not change their contents.",
        "For each missing path, output exactly this shape (one block per file):",
        "",
        "File: <path>",
        "```<ext>",
        "<complete file contents>",
        "```",
        "",
        "Missing owned_files:",
    ]
    for path in missing:
        lines.append(f"- {path}")
    lines.extend(["", "Owned scope (do not exceed):"])
    for path in owned:
        lines.append(f"- {path}")
    return "\n".join(lines)


@dataclass
class DeliveryAssessment:
    artifact_mode: bool
    owned: tuple[str, ...]
    json_ok: bool
    parsed_body: dict[str, Any] | None
    files: dict[str, str]
    missing: tuple[str, ...]
    truncated: bool
    error_type: str | None
    recovery: str
    method: str
    extract: dict[str, Any] | None


def assess_model_output(
    content: str | None,
    *,
    usage: Mapping[str, int] | None = None,
    finish_reason: str | None = None,
    owned_files: Sequence[str] | None = None,
    artifact_mode: bool = False,
) -> DeliveryAssessment:
    """同时走 JSON 解析和 ACS 抽取，给出共享错误类别。"""
    text = content if isinstance(content, str) else None
    truncated = is_true_output_truncation(usage, finish_reason)
    parsed, json_ok = parse_json_object(text)
    files: dict[str, str] = {}
    method = "none"
    if json_ok and parsed is not None:
        files.update(files_from_json_body(parsed))
        if files:
            method = "json"
    # 延迟导入：adapters 包初始化会回到 acceptance/fake_client。
    from .adapters.acs_text_extract import extract_delivery_map

    extract = extract_delivery_map(text or "")
    acs_files = extract.get("files") if isinstance(extract, Mapping) else None
    if isinstance(acs_files, Mapping):
        before = len(files)
        files = merge_files(
            files,
            {
                path: body
                for path, body in acs_files.items()
                if isinstance(path, str) and isinstance(body, str)
            },
        )
        if len(files) > before and method == "none":
            method = str(extract.get("method") or "acs")
    owned = normalize_paths(owned_files) if artifact_mode else ()
    missing = tuple(path for path in owned if path not in files) if artifact_mode else ()

    if not artifact_mode and json_ok:
        error: str | None = None
        recovery = RECOVERY_NONE
    elif artifact_mode and files and not missing:
        error = None
        recovery = RECOVERY_NONE
    elif artifact_mode and not owned and json_ok:
        error = None
        recovery = RECOVERY_NONE
    elif artifact_mode and files and missing:
        error = ERROR_PARTIAL_ARTIFACT
        recovery = RECOVERY_FORMAT_REPAIR_ONCE
    elif not files and truncated:
        error = ERROR_OUTPUT_TRUNCATED
        recovery = RECOVERY_NONE
    elif not files:
        error = ERROR_INVALID_FORMAT_NO_ARTIFACT
        recovery = RECOVERY_NONE if artifact_mode and not owned else RECOVERY_FORMAT_REPAIR_ONCE
    elif not artifact_mode:
        error = ERROR_OUTPUT_TRUNCATED if truncated else ERROR_INVALID_JSON
        recovery = RECOVERY_NONE
    else:
        error = None
        recovery = RECOVERY_NONE

    return DeliveryAssessment(
        artifact_mode=artifact_mode,
        owned=owned,
        json_ok=json_ok,
        parsed_body=dict(parsed) if parsed is not None else None,
        files=files,
        missing=missing,
        truncated=truncated,
        error_type=error,
        recovery=recovery,
        method=method,
        extract=dict(extract) if isinstance(extract, Mapping) else None,
    )


def should_run_format_repair(assessment: DeliveryAssessment) -> bool:
    """执行层闸门：类别允许修复，且当前请求带了非空 owned_files，且确实缺文件。"""
    if not assessment.artifact_mode or not assessment.owned:
        return False
    if assessment.recovery != RECOVERY_FORMAT_REPAIR_ONCE:
        return False
    return bool(assessment.missing)


@dataclass(frozen=True)
class RepairText:
    content: str | None
    usage: Mapping[str, int] | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class FinalDelivery:
    outcome: str
    error_type: str | None
    parsed_body: dict[str, Any] | None
    format_repair_used: bool
    files: dict[str, str]
    missing: tuple[str, ...]
    truncated: bool


def _error_after(
    base: DeliveryAssessment,
    files: Mapping[str, str],
    missing: tuple[str, ...],
    truncated: bool,
) -> str | None:
    artifact = base.artifact_mode
    if not artifact and base.json_ok:
        return None
    if artifact and not base.owned and base.json_ok:
        return None
    if artifact and files and not missing:
        return None
    if artifact and files and missing:
        return ERROR_OUTPUT_TRUNCATED if truncated else ERROR_PARTIAL_ARTIFACT
    if not files and truncated:
        return ERROR_OUTPUT_TRUNCATED
    if not files:
        return ERROR_INVALID_FORMAT_NO_ARTIFACT
    if not artifact:
        return ERROR_OUTPUT_TRUNCATED if truncated else ERROR_INVALID_JSON
    return None


def _render_body(
    base: DeliveryAssessment,
    files: Mapping[str, str],
    error: str | None,
    *,
    repair_used: bool,
    truncated: bool,
) -> dict[str, Any] | None:
    file_map = dict(files)
    if error is None and base.json_ok and isinstance(base.parsed_body, dict):
        # 文件集合没变时保留原始 JSON。一旦修复补了文件，改用 files 交付，
        # 避免 delivery=file 把后补的路径丢掉。
        if files_from_json_body(base.parsed_body) == file_map:
            return dict(base.parsed_body)
    if not file_map:
        return None
    notes = "format_repair" if repair_used else "acs_text_extract_recovery"
    body: dict[str, Any] = {
        "ok": True,
        "delivery": "files",
        "files": file_map,
        "notes": notes,
        "_truncated": truncated,
    }
    if base.extract is not None:
        body["_extract"] = base.extract
    return body


def _outcome_name(error: str | None, files: Mapping[str, str], artifact_mode: bool) -> str:
    if error is None:
        return "success"
    if artifact_mode and files and error in {ERROR_PARTIAL_ARTIFACT, ERROR_OUTPUT_TRUNCATED}:
        return "success"
    return "invalid_json"


def resolve_model_delivery(
    content: str | None,
    *,
    usage: Mapping[str, int] | None = None,
    finish_reason: str | None = None,
    owned_files: Sequence[str] | None = None,
    artifact_mode: bool = False,
    fetch_repair: Callable[[str], RepairText | None] | None = None,
) -> FinalDelivery:
    """分类，并在规则允许时恰好调用一次 ``fetch_repair``。失败后不再调用。"""
    base = assess_model_output(
        content,
        usage=usage,
        finish_reason=finish_reason,
        owned_files=owned_files,
        artifact_mode=artifact_mode,
    )
    files = dict(base.files)
    truncated = base.truncated
    repair_used = False
    if should_run_format_repair(base) and fetch_repair is not None:
        repair_used = True
        prompt = build_format_repair_prompt(base.owned, base.missing)
        fetched = fetch_repair(prompt)
        if fetched is not None and isinstance(fetched.content, str) and fetched.content:
            repair = assess_model_output(
                fetched.content,
                usage=fetched.usage,
                finish_reason=fetched.finish_reason,
                owned_files=base.owned,
                artifact_mode=True,
            )
            files = merge_files(base.files, repair.files)
            truncated = base.truncated or repair.truncated
    missing = tuple(path for path in base.owned if path not in files) if base.artifact_mode else ()
    error = _error_after(base, files, missing, truncated)
    body = _render_body(base, files, error, repair_used=repair_used, truncated=truncated)
    return FinalDelivery(
        outcome=_outcome_name(error, files, base.artifact_mode),
        error_type=error,
        parsed_body=body,
        format_repair_used=repair_used,
        files=dict(files),
        missing=missing,
        truncated=truncated,
    )
