"""Filesystem rendezvous for host-dispatched native agents; no network transport."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .fake_client import ApiRequest, ApiResponse, Outcome, RequestStatus, RequestValidationError


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


class NativeClient:
    model = "native-inherited"
    provider = "codex-native"

    def __init__(self, bridge_dir: Path | str, timeout_seconds: float = 1800):
        self.root = Path(bridge_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds

    def _paths(self, key):
        name = hashlib.sha256(key.encode()).hexdigest()
        return self.root / f"{name}.request.json", self.root / f"{name}.response.json"

    def call(self, request: ApiRequest) -> ApiResponse:
        source, destination = self._paths(request.idempotency_key)
        started = datetime.now(timezone.utc)
        if source.exists():
            previous = json.loads(source.read_text(encoding="utf-8"))
            if previous["input_artifact_hash"] != request.input_artifact_hash:
                raise RequestValidationError("native idempotency key is bound to another input")
        else:
            record = {**request.audit_fields(), "prompt": request.prompt,
                      "metadata": plain(request.metadata), "started_at": started.isoformat()}
            temporary = source.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, source)
        deadline = time.monotonic() + self.timeout_seconds
        while not destination.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        return self._read(request, started) if destination.exists() else self._timeout(request, started)

    async def acall(self, request):
        return await asyncio.to_thread(self.call, request)

    def _read(self, request, started):
        _, destination = self._paths(request.idempotency_key)
        record = json.loads(destination.read_text(encoding="utf-8"))
        body = record["body"]
        success = record.get("status", "success") == "success"
        return ApiResponse(
            request_id=record.get("producer", destination.stem), idempotency_key=request.idempotency_key,
            task_id=request.task_id, provider=self.provider, model=self.model,
            outcome=Outcome.SUCCESS if success else Outcome.SERVER_ERROR, status_code=200 if success else 500,
            raw_body=json.dumps(body, ensure_ascii=False), parsed_body=body, usage={}, error_type=None if success else "native_worker_error",
            retry_after_seconds=None, is_duplicate=False, duplicate_of=None, attempt=1,
            started_at=started, finished_at=datetime.now(timezone.utc), input_artifact_hash=request.input_artifact_hash or "")

    def _timeout(self, request, started):
        return ApiResponse(request_id="native-timeout", idempotency_key=request.idempotency_key,
                           task_id=request.task_id, provider=self.provider, model=self.model, outcome=Outcome.TIMEOUT,
                           status_code=0, raw_body=None, parsed_body=None, usage={}, error_type="timeout",
                           retry_after_seconds=None, is_duplicate=False, duplicate_of=None, attempt=1,
                           started_at=started, finished_at=datetime.now(timezone.utc), input_artifact_hash=request.input_artifact_hash or "")

    def request_status(self, key):
        source, destination = self._paths(key)
        if destination.exists():
            return RequestStatus.SENT
        return RequestStatus.UNKNOWN if source.exists() else RequestStatus.NOT_SENT

    def lookup_response(self, key):
        source, destination = self._paths(key)
        if not source.exists() or not destination.exists():
            return None
        record = json.loads(source.read_text(encoding="utf-8"))
        request = ApiRequest(task_id=record["task_id"], idempotency_key=key, prompt=record["prompt"],
                             model=self.model, provider=self.provider, input_artifact_hash=record["input_artifact_hash"])
        return self._read(request, datetime.fromisoformat(record["started_at"]))
