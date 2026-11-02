"""可改图 Executor 适配：在内核 Executor 之上插入 C 组拆合决策。

不修改 executor.py 本体；通过子类覆盖 `_release`，在派发前（以及完成/
失败/阻塞导致的下一轮 release）对未领取任务做候选生成→比选→原子改图。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from feasibility_scheduler.dag import build_dag
from feasibility_scheduler.executor import Executor
from feasibility_scheduler.models import Task
from feasibility_scheduler.statemachine import TaskState
from feasibility_scheduler.validation import validate_tasks

from .artifact_request import ArtifactMetadataExecutorMixin
from .contract_runtime import ContractExecutionMixin
from .graph_rewrite import RewriteCandidate, decide_rewrite

COMPONENT = "rewrite_adapter"


class GraphRewriteExecutor(ContractExecutionMixin, ArtifactMetadataExecutorMixin, Executor):
    """policy=rewrite 时使用；fixed 请继续用 PromptFromPayloadExecutor。

    额外覆盖 `_prompt`：与 legacy 适配一致，优先 payload["prompt_text"]。
    """

    def __init__(self, *args: Any, enable_rewrite: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.enable_rewrite = bool(enable_rewrite)
        self.rewrite_history: list[dict[str, Any]] = []
        self._rewrite_lock = asyncio.Lock()
        self._last_fingerprint: str | None = None

    def _prompt(self, task_id: str) -> str:
        task = self._task_by_id[task_id]
        text = task.payload.get("prompt_text") if task.payload else None
        if isinstance(text, str) and text.strip():
            return text
        return json.dumps(
            {"task_id": task.id, "payload": task.payload},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _state_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for task in self.tasks:
            try:
                out[task.id] = self.store.get_task(task.id).state.value
            except Exception:
                out[task.id] = TaskState.PENDING.value
        return out

    def _fingerprint(
        self, tasks: tuple[Task, ...] | list[Task], states: Mapping[str, str]
    ) -> str:
        parts = [
            f"{t.id}:{states.get(t.id, '?')}:{','.join(_owned_ids(t))}" for t in tasks
        ]
        return "|".join(parts)

    async def _release(self) -> list[str]:
        if self.enable_rewrite:
            await self._maybe_rewrite(trigger="before_release")
        return await super()._release()

    async def _maybe_rewrite(self, *, trigger: str) -> RewriteCandidate | None:
        async with self._rewrite_lock:
            states = self._state_map()
            fp = self._fingerprint(self.tasks, states)
            if fp == self._last_fingerprint and self.rewrite_history:
                last = self.rewrite_history[-1]
                if last.get("kind") == "keep" and last.get("fingerprint") == fp:
                    return None

            selected, all_cands = decide_rewrite(self.tasks, states=states)
            for cand in all_cands:
                self.log.record(
                    "candidate_generated",
                    component=COMPONENT,
                    candidate_id=cand.candidate_id,
                    decision_reason=cand.reason,
                    details=cand.to_dict(),
                )
                self.log.record(
                    "candidate_validated",
                    component=COMPONENT,
                    candidate_id=cand.candidate_id,
                    decision_reason="通过适配层合法性检查",
                    details={"kind": cand.kind, "score": cand.score},
                )

            self.log.record(
                "candidate_selected",
                component=COMPONENT,
                candidate_id=selected.candidate_id,
                decision_reason=selected.reason,
                details={
                    "trigger": trigger,
                    "score": selected.score,
                    "kind": selected.kind,
                    "alternatives": [c.candidate_id for c in all_cands],
                },
            )

            entry = {
                "trigger": trigger,
                "kind": selected.kind,
                "candidate_id": selected.candidate_id,
                "reason": selected.reason,
                "fingerprint": fp,
                "removed_ids": list(selected.removed_ids),
                "added_ids": list(selected.added_ids),
            }
            self.rewrite_history.append(entry)

            if selected.kind == "keep":
                self._last_fingerprint = fp
                return selected

            self._apply_candidate(selected)
            self._last_fingerprint = self._fingerprint(self.tasks, self._state_map())
            return selected

    def _apply_candidate(self, selected: RewriteCandidate) -> None:
        errors = list(validate_tasks(selected.tasks))
        if errors:
            raise RuntimeError(f"改图后任务图非法：{errors}")
        new_graph = build_dag(selected.tasks)

        # 不把被替换任务迁到 CANCELLED：Executor 的 EventLog 未走 _step 时
        # 会与 store 迁移序列不一致，导致 check_trace 失败。被替换任务留在
        # store 原状态，但从 self.tasks / graph 移除，不再派发；judge_run 只
        # 验收改图后的活动任务集。
        for task in selected.tasks:
            if task.id in selected.added_ids:
                try:
                    self.store.get_task(task.id)
                except Exception:
                    self.store.create_task(task.id)
                    self.log.record(
                        "task_validated",
                        component=COMPONENT,
                        task_id=task.id,
                        previous_state=None,
                        new_state=TaskState.PENDING,
                        decision_reason="改图新增任务，输入校验通过",
                        details={"rewrite": True, "candidate_id": selected.candidate_id},
                        candidate_id=selected.candidate_id,
                    )

        self.tasks = tuple(selected.tasks)
        self._task_by_id = {t.id: t for t in self.tasks}
        self.graph = new_graph

        by_id = getattr(self.client, "_by_id", None)
        if isinstance(by_id, dict):
            by_id.clear()
            by_id.update(self._task_by_id)

        self.log.record(
            "graph_rewrite_applied",
            component=COMPONENT,
            candidate_id=selected.candidate_id,
            decision_reason=selected.reason,
            details={
                "removed_ids": list(selected.removed_ids),
                "added_ids": list(selected.added_ids),
                "task_ids": [t.id for t in self.tasks],
                "kind": selected.kind,
                "score": selected.score,
            },
        )


def _owned_ids(task: Task) -> tuple[str, ...]:
    raw = task.payload.get("owned_files") if task.payload else None
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(x) for x in raw if isinstance(x, str))
