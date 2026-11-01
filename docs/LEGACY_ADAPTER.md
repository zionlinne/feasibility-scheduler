# 旧题适配层（不破坏内核）

把 ACS 旧题《离线协作文档、版本与权限服务》的 `TASK_INPUT.json` 模块图接到本包 `Executor`。

## 启动

```bat
cd /d D:\ai娘们\算法工程
python scripts\run_legacy_doc_task.py
python scripts\run_legacy_doc_task.py --policy rewrite --client fake
python scripts\run_legacy_doc_task.py --policy fixed --client fake
python scripts\run_legacy_doc_task.py --client http --modules users,spaces
python scripts\run_legacy_doc_task.py --client http --acceptance node-check
```

- 默认 `--client fake`：适配层占位 client，不触网、不改 `FakeClient`。
- `--client http`：`create_client("http")`，密钥来自环境变量或 DPAPI 默认文件。
- **`--policy rewrite`（默认）**：走可改图路径。同初始 DAG；派发前及完成/失败/阻塞后的下一轮调度，对未领取任务生成 keep/split/merge 候选，比选记理由后原子改图，再按共同并发上限派发。拆合沿真实 `owned_files` 边界，禁止改名/说明文件假拆合。
- **`--policy fixed`**：固定初始 DAG（旧瘦适配行为），不做运行时改图。
- 共同层并发上限默认 **8**（`--max-concurrency`），由 Executor 动态并行领取。
- 产物目录：`runs/legacy-doc-<timestamp>/`（`workspace/`、`events.jsonl`、`state.sqlite3`、`summary.json`）。`summary.json` 含 `policy`、`rewrite_history`。

## 与固定 DAG 适配的差别

| | fixed | rewrite |
|---|---|---|
| 初始图 | TASK_INPUT modules | 相同 |
| 运行中改图 | 否 | 是（adapters `graph_rewrite` + `GraphRewriteExecutor`） |
| 事件 | 常规执行事件 | 额外 `candidate_*` / `graph_rewrite_applied` |
| 内核 | 不改 | 不改（仅子类覆盖 `_release`/`_prompt`） |

## 局限

- 旧题完整 33 ops / CLI / HTTP / 持久化验收**未**接入；当前 `loose` 只验 JSON `{ok,files}` 并落盘，`node-check` 额外 `node --check`。
- 模型必须返回 JSON 对象；纯代码块会被 HttpClient 判为 `invalid_json`。
- 内核 Stage1 仍将「候选图拆分合并」标为暂缓；本适配层在 **adapters** 实现 C 组风格决策入口，**不是**往 dag/phi/timing/gate/statemachine/store/events/acceptance 内核塞算法。
- 改图启发式：轻量模块（`estimated_tokens≤3000`）可合并；≥2 个源码文件的未领取模块可按文件拆分。比选为确定性打分，非全局最优证明。
