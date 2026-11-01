"""旧题适配层：把 ACS TASK_INPUT 模块图接到本包 Executor，不改内核。"""

from .legacy_doc import (
    LegacyDocConfig,
    build_legacy_tasks,
    legacy_artifact_acceptor,
    load_task_input,
    make_placeholder_client,
    PromptFromPayloadExecutor,
    run_legacy_doc,
)
from .graph_rewrite import (
    RewriteCandidate,
    decide_rewrite,
    generate_candidates,
    select_candidate,
)
from .rewrite_executor import GraphRewriteExecutor
from . import acs_text_extract

__all__ = [
    "LegacyDocConfig",
    "PromptFromPayloadExecutor",
    "GraphRewriteExecutor",
    "RewriteCandidate",
    "build_legacy_tasks",
    "decide_rewrite",
    "generate_candidates",
    "legacy_artifact_acceptor",
    "load_task_input",
    "make_placeholder_client",
    "run_legacy_doc",
    "select_candidate",
    "acs_text_extract",
]
