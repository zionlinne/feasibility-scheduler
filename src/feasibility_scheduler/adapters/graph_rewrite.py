"""C 组风格图改写决策（适配层，不改内核）。

对外约定
--------
- 同初始 DAG；在派发前 / 完成·失败·阻塞后，对**未领取**任务生成
  keep / split / merge 候选，比选记理由后原子改图，再按共同并发上限派发。
- 拆合必须沿真实 ``owned_files`` 模块边界：互不改同一文件；禁止靠改名或
  说明文件做假拆合；合并不得丢失原有文件所有权。
- 本模块只产出候选与新 Task 列表；真正改 store/graph 由 RewriteExecutor 完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from feasibility_scheduler.dag import build_dag
from feasibility_scheduler.models import Task
from feasibility_scheduler.validation import validate_tasks

#: 轻量模块阈值阈值（与旧题 estimated_tokens 同量级）；两端均 ≤ 此值才考虑合并。
LIGHT_TOKEN_THRESHOLD = 3000

#: 视为「可独立程序模块」的源码后缀；仅 README/说明不算合法假拆目标的唯一文件。
_CODE_SUFFIXES = (".mjs", ".js", ".cjs", ".ts", ".py")


@dataclass(frozen=True)
class RewriteCandidate:
    """一次候选改图。"""

    candidate_id: str
    kind: str  # keep | split | merge | recovery_split
    reason: str
    score: float
    # 应用后完整任务列表（含未改动的任务）
    tasks: tuple[Task, ...]
    # 被替换掉的旧任务 id
    removed_ids: tuple[str, ...] = ()
    # 新出现的任务 id
    added_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "reason": self.reason,
            "score": self.score,
            "removed_ids": list(self.removed_ids),
            "added_ids": list(self.added_ids),
            "task_ids": [t.id for t in self.tasks],
            "details": dict(self.details),
        }


def _tokens(task: Task) -> int:
    raw = task.payload.get("estimated_tokens") if task.payload else None
    if isinstance(raw, (int, float)) and raw >= 0:
        return int(raw)
    owned = _owned(task)
    # 无估计时按文件数给弱先验，避免全 0
    return max(500, 800 * max(1, len(owned)))


def _owned(task: Task) -> tuple[str, ...]:
    raw = task.payload.get("owned_files") if task.payload else None
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(x) for x in raw if isinstance(x, str) and x.strip())


def _is_code_file(path: str) -> bool:
    lower = path.lower().replace("\\", "/")
    return any(lower.endswith(suf) for suf in _CODE_SUFFIXES)


def _mutable_ids(
    tasks: Sequence[Task],
    *,
    states: Mapping[str, str],
) -> set[str]:
    """未领取且非终态的任务才可改。"""
    frozen = {
        "CLAIMED",
        "RUNNING",
        "RESULT_RECEIVED",
        "ACCEPTANCE_PENDING",
        "ACCEPTED",
        "RETRY_WAIT",
        "FAILED",
        "REJECTED",
        "RECOVERY_REQUIRED",
        "CANCELLED",
    }
    out: set[str] = set()
    for task in tasks:
        st = states.get(task.id, "PENDING")
        if st not in frozen:
            out.add(task.id)
    return out


def _clone_task(
    task: Task,
    *,
    new_id: str | None = None,
    dependencies: tuple[str, ...] | None = None,
    owned_files: Sequence[str] | None = None,
    title: str | None = None,
    estimated_tokens: int | None = None,
    prompt_text: str | None = None,
) -> Task:
    payload = dict(task.payload or {})
    tid = new_id or task.id
    if owned_files is not None:
        payload["owned_files"] = list(owned_files)
        payload["outputs"] = list(owned_files)
    if title is not None:
        payload["title"] = title
    if estimated_tokens is not None:
        payload["estimated_tokens"] = estimated_tokens
    payload["module_id"] = tid
    deps = dependencies if dependencies is not None else task.dependencies
    payload["deps"] = list(deps)
    if prompt_text is not None:
        payload["prompt_text"] = prompt_text
    return Task(
        id=tid,
        dependencies=tuple(deps),
        constraints=task.constraints,
        constraints_declared=task.constraints_declared,
        output_contract=dict(task.output_contract) if task.output_contract else None,
        payload=payload,
    )


def _retarget_deps(
    tasks: Sequence[Task],
    *,
    mapping: Mapping[str, tuple[str, ...]],
) -> list[Task]:
    """把依赖里出现的旧 id 按 mapping 展开成新 id 集合。"""
    rebuilt: list[Task] = []
    for task in tasks:
        new_deps: list[str] = []
        seen: set[str] = set()
        for dep in task.dependencies:
            targets = mapping.get(dep, (dep,))
            for t in targets:
                if t not in seen and t != task.id:
                    seen.add(t)
                    new_deps.append(t)
        if tuple(new_deps) == task.dependencies:
            rebuilt.append(task)
        else:
            rebuilt.append(_clone_task(task, dependencies=tuple(new_deps)))
    return rebuilt


def _validate_or_none(tasks: Sequence[Task]) -> tuple[Task, ...] | None:
    errors = list(validate_tasks(tasks))
    if errors:
        return None
    try:
        build_dag(tasks)
    except Exception:
        return None
    # 所有权：同一文件不能被两个活跃任务拥有
    seen: set[str] = set()
    for task in tasks:
        for path in _owned(task):
            key = path.replace("\\", "/")
            if key in seen:
                return None
            seen.add(key)
    from .contract_plan import refresh_contract
    return tuple(refresh_contract(task) for task in tasks)


def _cover_files(tasks: Sequence[Task]) -> set[str]:
    out: set[str] = set()
    for task in tasks:
        for path in _owned(task):
            out.add(path.replace("\\", "/"))
    return out


def _keep_candidate(tasks: Sequence[Task]) -> RewriteCandidate:
    return RewriteCandidate(
        candidate_id="keep",
        kind="keep",
        reason="保持当前 DAG，不做拆合",
        score=0.0,
        tasks=tuple(tasks),
        details={"action": "keep"},
    )


def _merge_pair(
    tasks: Sequence[Task],
    left: Task,
    right: Task,
    *,
    serial: int,
) -> RewriteCandidate | None:
    """合并两个未领取、文件不交叠的真实模块。"""
    owned_l = _owned(left)
    owned_r = _owned(right)
    if left.payload.get("disable_merge") or right.payload.get("disable_merge"):
        return None
    if left.payload.get("contract_hash") != right.payload.get("contract_hash"):
        return None
    if not owned_l or not owned_r:
        return None
    set_l = {p.replace("\\", "/") for p in owned_l}
    set_r = {p.replace("\\", "/") for p in owned_r}
    if set_l & set_r:
        return None  # 假/冲突边界
    # 禁止把说明文件单独当成「合并收益」核心；至少一侧要有源码文件
    if not any(_is_code_file(p) for p in list(owned_l) + list(owned_r)):
        return None

    merged_id = f"merge_{left.id}_{right.id}"
    if any(t.id == merged_id for t in tasks):
        merged_id = f"merge_{left.id}_{right.id}_{serial}"

    # 合并后依赖 = 两侧依赖去掉彼此
    dep_set = []
    seen: set[str] = set()
    for dep in list(left.dependencies) + list(right.dependencies):
        if dep in {left.id, right.id}:
            continue
        if dep not in seen:
            seen.add(dep)
            dep_set.append(dep)

    tokens_l, tokens_r = _tokens(left), _tokens(right)
    merged_owned = list(owned_l) + list(owned_r)
    merged = _clone_task(
        left,
        new_id=merged_id,
        dependencies=tuple(dep_set),
        owned_files=merged_owned,
        title=f"merged:{left.id}+{right.id}",
        estimated_tokens=tokens_l + tokens_r,
    )
    # prompt 由调用方在 apply 后统一重写；此处先占位保留 left 的 prompt 结构标记
    payload = dict(merged.payload)
    payload["merged_from"] = [left.id, right.id]
    if payload.get("contract_hash"):
        payload["contract_module_ids"] = sorted(set(
            left.payload["contract_module_ids"] + right.payload["contract_module_ids"]
        ))
    payload["prompt_text"] = (
        f"[REWRITE-MERGE] Implement modules {left.id} + {right.id} together.\n"
        f"owned_files={merged_owned}\n"
        + str(payload.get("prompt_text") or "")
    )
    merged = Task(
        id=merged.id,
        dependencies=merged.dependencies,
        constraints=merged.constraints,
        constraints_declared=merged.constraints_declared,
        output_contract=merged.output_contract,
        payload=payload,
    )

    remaining = [t for t in tasks if t.id not in {left.id, right.id}]
    remaining.append(merged)
    mapping = {left.id: (merged_id,), right.id: (merged_id,)}
    remaining = _retarget_deps(remaining, mapping=mapping)

    before_files = _cover_files(tasks)
    after = _validate_or_none(remaining)
    if after is None:
        return None
    if _cover_files(after) != before_files:
        return None  # 合并不丢文件

    # 小成本优先：两侧越轻、节省的协调槽位越多，分越高
    light_bonus = 0.0
    if tokens_l <= LIGHT_TOKEN_THRESHOLD and tokens_r <= LIGHT_TOKEN_THRESHOLD:
        light_bonus = 2.0
    # 同依赖集合的兄弟模块更像真边界合并
    sibling_bonus = 1.0 if set(left.dependencies) == set(right.dependencies) else 0.0
    # 成本：合并后并行度损失近似按较小一侧 tokens
    delta_u = light_bonus + sibling_bonus + 1.0
    delta_c = max(1.0, min(tokens_l, tokens_r) / 1000.0)
    score = delta_u / delta_c

    return RewriteCandidate(
        candidate_id=f"merge:{left.id}+{right.id}",
        kind="merge",
        reason=(
            f"合并轻量真实模块 {left.id} 与 {right.id} "
            f"(tokens={tokens_l}+{tokens_r}，owned 不交叠)"
        ),
        score=score,
        tasks=after,
        removed_ids=(left.id, right.id),
        added_ids=(merged_id,),
        details={
            "left": left.id,
            "right": right.id,
            "merged_id": merged_id,
            "owned_files": merged_owned,
            "tokens": [tokens_l, tokens_r],
        },
    )


def _split_module(
    tasks: Sequence[Task],
    task: Task,
    *,
    serial: int,
) -> RewriteCandidate | None:
    """按 owned_files 真拆：每个源码文件一个子任务；禁止空拆 / 改名假拆。"""
    owned = _owned(task)
    code_files = [p for p in owned if _is_code_file(p)]
    other_files = [p for p in owned if not _is_code_file(p)]
    # 至少 2 个源码文件才允许拆；说明文件附着到第一个子任务，不单独假拆
    if len(code_files) < 2:
        return None

    children: list[Task] = []
    child_ids: list[str] = []
    tokens_total = _tokens(task)
    per = max(1, tokens_total // len(code_files))
    for idx, path in enumerate(code_files):
        cid = f"{task.id}__f{idx}_{_slug(path)}"
        if any(t.id == cid for t in tasks):
            cid = f"{cid}_{serial}"
        files = [path]
        if idx == 0 and other_files:
            files = [path] + list(other_files)
        child = _clone_task(
            task,
            new_id=cid,
            dependencies=task.dependencies,
            owned_files=files,
            title=f"split:{task.id}:{path}",
            estimated_tokens=per + (len(other_files) * 100 if idx == 0 else 0),
        )
        payload = dict(child.payload)
        payload["split_from"] = task.id
        payload["prompt_text"] = (
            f"[REWRITE-SPLIT] Implement file(s) {files} from module {task.id}.\n"
            + str(payload.get("prompt_text") or "")
        )
        child = Task(
            id=child.id,
            dependencies=child.dependencies,
            constraints=child.constraints,
            constraints_declared=child.constraints_declared,
            output_contract=child.output_contract,
            payload=payload,
        )
        children.append(child)
        child_ids.append(cid)

    remaining = [t for t in tasks if t.id != task.id]
    remaining.extend(children)
    mapping = {task.id: tuple(child_ids)}
    remaining = _retarget_deps(remaining, mapping=mapping)

    before_files = _cover_files(tasks)
    after = _validate_or_none(remaining)
    if after is None:
        return None
    if _cover_files(after) != before_files:
        return None

    # 重模块多文件拆分：并行收益高
    delta_u = float(len(code_files)) + (2.0 if tokens_total > LIGHT_TOKEN_THRESHOLD else 0.5)
    delta_c = 1.0 + 0.25 * len(code_files)  # 协调开销
    score = delta_u / delta_c

    return RewriteCandidate(
        candidate_id=f"split:{task.id}",
        kind="split",
        reason=(
            f"按真实文件边界拆分 {task.id} → {len(child_ids)} 个子任务 "
            f"(code_files={code_files})"
        ),
        score=score,
        tasks=after,
        removed_ids=(task.id,),
        added_ids=tuple(child_ids),
        details={
            "parent": task.id,
            "children": child_ids,
            "code_files": code_files,
            "attached_non_code_to": child_ids[0] if other_files else None,
        },
    )


def _slug(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    out = []
    for ch in name:
        if ch.isalnum() or ch in {"_", "-"}:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:40] or "file"


def recover_failed_merge(tasks: Sequence[Task], task: Task) -> RewriteCandidate | None:
    """Bounded fallback after confirmed server-error exhaustion, not ambiguous timeouts."""
    if not task.payload.get("merged_from") or task.payload.get("merge_recovery_depth", 0) >= 1:
        return None
    split = _split_module(tasks, task, serial=1)
    if split is None:
        return None
    recovered = []
    for child in split.tasks:
        if child.id in split.added_ids:
            child = Task(id=child.id, dependencies=child.dependencies, constraints=child.constraints,
                         constraints_declared=child.constraints_declared, output_contract=child.output_contract,
                         payload={**child.payload, "disable_merge": True, "merge_recovery_depth": 1,
                                  "recovered_from": task.id})
        recovered.append(child)
    after = _validate_or_none(recovered)
    if after is None:
        return None
    return RewriteCandidate(candidate_id=f"recover-split:{task.id}", kind="recovery_split", reason="合并任务请求失败耗尽，按文件拆回并禁止重新合并",
                            score=split.score, tasks=after, removed_ids=split.removed_ids, added_ids=split.added_ids,
                            details={"failed_parent": task.id, "bounded_recovery": True})


def generate_candidates(
    tasks: Sequence[Task],
    *,
    states: Mapping[str, str],
) -> list[RewriteCandidate]:
    """生成 keep + 合法 split/merge 候选（未领取任务）。"""
    task_list = list(tasks)
    mutable = _mutable_ids(task_list, states=states)
    candidates: list[RewriteCandidate] = [_keep_candidate(task_list)]
    by_id = {t.id: t for t in task_list}
    mutable_tasks = [by_id[i] for i in sorted(mutable) if i in by_id]

    serial = 0
    for task in mutable_tasks:
        plan = task.payload.get("contract_plan", {})
        for proposal in plan.get("decompositions", ()):
            if proposal["parent"] != task.id:
                continue
            declarations = {m["id"]: m for m in plan.get("modules", ())}
            children = [_clone_task(task, new_id=child["id"], owned_files=child["owned_files"],
                                    dependencies=tuple(dict.fromkeys(task.dependencies + tuple(
                                        declarations.get(child["id"], {}).get("execution_dependencies", ())))),
                                    title=child["postcondition"], estimated_tokens=max(1, _tokens(task) // len(proposal["children"])))
                        for child in proposal["children"]]
            if any(child.id in by_id for child in children):
                continue
            rebuilt = _retarget_deps([t for t in task_list if t.id != task.id] + children,
                                     mapping={task.id: tuple(c.id for c in children)})
            after = _validate_or_none(rebuilt)
            if after is not None and _cover_files(after) == _cover_files(task_list):
                candidates.append(RewriteCandidate(
                    candidate_id=f"contract-split:{task.id}", kind="split",
                    reason="按契约提议的交付物边界拆分并重新投影检查责任", score=float(len(children)),
                    tasks=after, removed_ids=(task.id,), added_ids=tuple(c.id for c in children),
                    details={"source": "contract_plan"}))
    # merge：轻量 + 文件不交叠
    for i, left in enumerate(mutable_tasks):
        for right in mutable_tasks[i + 1 :]:
            if _tokens(left) > LIGHT_TOKEN_THRESHOLD or _tokens(right) > LIGHT_TOKEN_THRESHOLD:
                continue
            # 若存在相互依赖则不合并（避免把链压成环风险由校验兜底，这里直接跳过）
            if left.id in right.dependencies or right.id in left.dependencies:
                continue
            serial += 1
            cand = _merge_pair(task_list, left, right, serial=serial)
            if cand is not None:
                candidates.append(cand)

    for task in mutable_tasks:
        serial += 1
        cand = _split_module(task_list, task, serial=serial)
        if cand is not None:
            candidates.append(cand)

    return candidates


def select_candidate(candidates: Sequence[RewriteCandidate]) -> RewriteCandidate:
    """比选：最高分；同分 keep 优先，再按 candidate_id 稳定排序。"""
    if not candidates:
        raise ValueError("candidates 为空")
    return sorted(
        candidates,
        key=lambda c: (
            c.score,
            1 if c.kind == "keep" else 0,
            c.candidate_id,
        ),
        reverse=True,
    )[0]


def decide_rewrite(
    tasks: Sequence[Task],
    *,
    states: Mapping[str, str],
) -> tuple[RewriteCandidate, list[RewriteCandidate]]:
    """生成并选出最佳候选；返回 (selected, all)。"""
    all_cands = generate_candidates(tasks, states=states)
    # 给每个非 keep 候选再跑一遍「假拆合」门：added 必须带来真实 owned 变化
    filtered: list[RewriteCandidate] = []
    for cand in all_cands:
        if cand.kind == "keep":
            filtered.append(cand)
            continue
        if not cand.removed_ids or not cand.added_ids:
            continue
        filtered.append(cand)
    selected = select_candidate(filtered)
    return selected, filtered
