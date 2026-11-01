#!/usr/bin/env python3
"""用本包逻辑跑旧题《离线协作文档、版本与权限服务》。

默认 Fake（占位 client，不触网）；``--client http`` 才打 88api。

示例
----
python scripts/run_legacy_doc_task.py
python scripts/run_legacy_doc_task.py --client http --modules users,spaces
python scripts/run_legacy_doc_task.py --client http --acceptance node-check
python scripts/run_legacy_doc_task.py --policy rewrite --max-concurrency 8
python scripts/run_legacy_doc_task.py --policy fixed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from feasibility_scheduler.adapters.legacy_doc import (  # noqa: E402
    LegacyDocConfig,
    default_paths,
    run_legacy_doc,
)


def _parse_modules(raw: str | None) -> tuple[str, ...] | None:
    if raw is None or not raw.strip():
        return None
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or None


def main(argv: list[str] | None = None) -> int:
    defaults = default_paths()
    parser = argparse.ArgumentParser(
        description="Legacy ACS doc task → feasibility_scheduler adapter"
    )
    parser.add_argument(
        "--task-input",
        type=Path,
        default=defaults["task_input"],
        help="TASK_INPUT.json 路径（默认 ACS round2 实验产物，只读）",
    )
    parser.add_argument(
        "--task-md",
        type=Path,
        default=defaults["task_md"],
        help="TASK.md 路径",
    )
    parser.add_argument(
        "--client",
        choices=("fake", "http"),
        default="fake",
        help="fake=占位（默认）；http=真实 88api",
    )
    parser.add_argument(
        "--policy",
        choices=("rewrite", "fixed"),
        default="rewrite",
        help="rewrite=可改图拆合决策（默认）；fixed=固定初始 DAG",
    )
    parser.add_argument(
        "--acceptance",
        choices=("loose", "node-check"),
        default="loose",
        help="loose=检查 JSON+落盘；node-check=额外 node --check",
    )
    parser.add_argument(
        "--modules",
        default=None,
        help="逗号分隔 module_id 切片，例如 users,spaces；默认全图",
    )
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--model",
        default=None,
        help="覆盖 FEASIBILITY_MODEL / HttpClient 默认模型",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="运行目录；默认 runs/legacy-doc-<timestamp>",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="单次 HTTP 请求超时（秒）",
    )
    args = parser.parse_args(argv)

    if not args.task_input.is_file():
        print(f"ERROR: TASK_INPUT 不存在: {args.task_input}", file=sys.stderr)
        return 2
    if not args.task_md.is_file():
        print(f"ERROR: TASK.md 不存在: {args.task_md}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = args.workdir or (ROOT / "runs" / f"legacy-doc-{stamp}")

    cfg = LegacyDocConfig(
        task_input_path=args.task_input,
        task_md_path=args.task_md,
        workdir=workdir,
        client_kind=args.client,
        acceptance=args.acceptance,
        policy=args.policy,
        max_concurrency=args.max_concurrency,
        max_attempts=args.max_attempts,
        module_ids=_parse_modules(args.modules),
        model=args.model,
        timeout_seconds=args.timeout_seconds,
    )

    print(
        json.dumps(
            {
                "workdir": str(cfg.workdir),
                "client": cfg.client_kind,
                "acceptance": cfg.acceptance,
                "policy": cfg.policy,
                "modules": list(cfg.module_ids) if cfg.module_ids else "ALL",
                "max_concurrency": cfg.max_concurrency,
            },
            ensure_ascii=False,
        )
    )

    outcome = run_legacy_doc(cfg)
    print(
        json.dumps(
            {
                "ok": outcome.ok,
                "exit_code": outcome.exit_code,
                "states": {k: v.value for k, v in outcome.states.items()},
                "reasons": list(outcome.verdict.reasons),
                "summary": str(workdir / "summary.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
