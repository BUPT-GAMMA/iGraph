# Question Benchmark 指南

## 1. 题库布局与结构

runner 兼容以下题库目录结构：

```text
<questions-root>/
├── theory/<dataset>/questions.json
├── retrieval/<dataset>/questions.json
├── ml/<dataset>/
│   ├── questions.json
│   └── pure_ml_bench_masked_data.pt
├── insight/<dataset>/questions.json
├── prediction/<dataset>/
│   ├── questions.json
│   └── intermediate_ground_truth_masked_data.pt
└── dataset/<dataset>.pt
```

标准文件必须使用以下结构：

```json
{
  "schema": "igraph.benchmark.questions",
  "schema_version": "1.0",
  "category": "theory",
  "dataset": "cora",
  "metadata": {},
  "tasks": [
    {
      "id": "task_0001",
      "question": "...",
      "task_type": "...",
      "required_tools": ["nx.density"],
      "ground_truth": {"subgraph": {}, "answer": 0.25},
      "context": {}
    }
  ]
}
```

category、dataset 与目录名必须一致。runner 也兼容旧式顶层数组、`questions`、`benchmark` 和单题对象，
但新数据应始终使用标准 schema。复合题号固定为 `category/dataset/id`。

### ML / prediction 的 PT 路由

`ml` 与 `prediction` 的题目输入来自各自 `questions.json` 同目录的 masked PT，不能替换为
`dataset/<dataset>.pt`：

| category | masked PT | `--dataset-root` 的作用 |
|---|---|---|
| `ml` | `pure_ml_bench_masked_data.pt` | 提供基础图 `<dataset>.pt` |
| `prediction` | `intermediate_ground_truth_masked_data.pt` | 提供基础图 `<dataset>.pt` |

loader 为每道题保留它在原 `questions.json` 中的零基 `source_index`。masked dict 必须用这个稳定索引取
`{"labels": ..., "edge_index": ...}`，所以 category/dataset 筛选、shuffle、全局题数限制和并发完成顺序
都不会改变 PT 对应关系。

在 `--agent-command-json` 命令模式中，runner 将选中的 masked 内容按参考流程注入基础图：NC 更新标签，
LP 更新边，GC 容器更新 `graphs[context.graph_idx]` 中对应图的标签（GC 的 masked edge 不注入）。每个
attempt 单独物化一个临时 PT 并通过 `dataset_path` 传给 agent，因此题内并行不会共享可写输入；该 agent
退出后立即删除其精确临时文件。attempt artifact 保存 base/masked 路径、稳定索引、任务类型和应用字段，
可确定性重建输入。`--validate-only` 只验证绑定和文件存在性，`--replay-file` 不需要物化。

对很大的 masked PT，runner 强制使用 `torch.load(mmap=True, weights_only=True)`，并按规范化路径和文件
stat 做线程安全的进程级缓存。这样不会为每道题反复加载存储，但 mmap-backed dict 仍是整个 checkpoint
的映射，不代表文件格式支持真正的单 key 随机读取。整批题结束后缓存会释放；基础图的每题临时物化仍会
产生磁盘 I/O。

选择参数：

- `--categories theory retrieval ml insight prediction`
- `--datasets cora citeseer`
- `--questions-per-file N`：每个文件上限，`0` 表示不限；
- `--question-count N`：全局上限，默认 `1`，`0` 表示不限；
- `--questions-per-type N`：每个正式 benchmark cell 精确选择 N 题；启用后不再应用默认全局上限；
- `--shuffle --seed 42`：先做可复现 shuffle，再应用上限。

`--questions-per-type` 与最终 `benchmark_matrix` 共用分类器，分组键为
`problem_group/graph_family/task_subtype`。它不能和正数 `--question-count` 或
`--questions-per-file` 同时使用。loader 会先扫描并验证所有匹配题目；启用 `--shuffle` 时，再对每个
正式 cell 做 seeded reservoir sampling。原始 `source_index` 不会改变。任一预期 cell 完全缺失、题量少于 N、类型为
`UNKNOWN` 或属于图类型不支持的任务组合时，CLI 在启动 agent 前返回 `input_error`，不会用较小类别的
实际数量悄悄降级。不传 `--shuffle` 时每个 cell 选择题库顺序中的前 N 题；传入 `--shuffle` 后才会进行
seeded reservoir sampling，并在最后对所选题目做全局 seeded shuffle。显式指定数据集时，每个已选类别
都必须存在对应的 `questions.json`；多个同属一种 graph family 的数据集则按最终 matrix cell 合并成一个
抽样池，不是每个数据集各取 N 题。

对 Cora 全五类，正式 cell 共 10 个：Theory 和 Retrieval 各有 Node/Edge/Graph，ML 有 NC/LP，OpenQA
有 Insight/Prediction。因此每类 N 题时总数严格为 `10N`；当前最小类别 Retrieval/Edge 有 28 题，N
最大为 28。运行结果的 `selection.sampling.available_type_counts` 与
`selection.sampling.selected_type_counts` 会记录抽样前后计数，stdout receipt 的 `sampling` 同步返回。

## 2. Agent 适配协议

runner 对 agent adapter 使用无 shell 的子进程协议。stdin 是：

```json
{
  "schema": "igraph.benchmark.agent_task.v1",
  "question_id": "theory/cora/task_0001",
  "id": "task_0001",
  "category": "theory",
  "dataset": "cora",
  "task_type": "...",
  "question": "...",
  "context": {},
  "dataset_path": "/.../workspace/cora.pt",
  "attempt": 1,
  "execution": {"intra_task_parallelism": 2}
}
```

ground truth、`required_tools` 和评分用的 `accepted_tool_paths` 不会传给 agent，防止答案/评分路径直接
泄漏。每个 attempt 都有独立工作目录和独立物化输入；题库 `source_file`、`source_index`、masked 源路径
及 masked 索引只会在整个 agent 阶段结束后写入最终 artifact。以下环境变量同时可用：

- `IGRAPH_BENCHMARK_TASK_FILE`
- `IGRAPH_DATASET_PATH`
- `IGRAPH_ATTEMPT_PARALLELISM`
- `IGRAPH_INTRA_TASK_PARALLELISM`
- `IGRAPH_VIRTUAL_ML_BROKER_ENDPOINT`（仅虚拟 ML command 模式）
- `IGRAPH_VIRTUAL_ML_BROKER_TOKEN`（仅虚拟 ML command 模式）
- `IGRAPH_GRAPH_ML_TOOL`

adapter 成功时应在 stdout 输出一个 JSON 对象：

```json
{
  "status": "success",
  "final_answer": 0.25,
  "reasoning": "concise solution record",
  "tool_calls": [
    {
      "name": "get_density",
      "arguments": {},
      "result": {"value": 0.25},
      "success": true
    }
  ],
  "transcript": []
}
```

也可以直接输出 OpenAI/Codex 风格 JSONL transcript；runner 会完整读取而不是只取最后一行，并按
`call_id` 配对 `function_call`/`function_call_output`。非零退出、超时、空输出和非法 JSON 都会形成结构化
失败记录，不会中止其他题目。最终答案必须是严格 JSON，不接受 Markdown fence 或 `NaN/Infinity`。

## 3. 并行与题数控制

`questions` 子命令同时支持任务间并行与任务内并行：

- `--task-parallelism P`：最多并行处理 `P` 道不同题；
- `--attempts-per-question K`：每题运行 `K` 个独立 attempt；
- `--intra-task-parallelism Q`：同题最多并行 `Q` 个 attempt；
- 最大 agent 进程并发为 `P × min(Q, K)`；
- `--question-count N`：全局题数，默认 `1`，`0` 表示全部；
- `--questions-per-file N`：每个 `questions.json` 的上限，`0` 表示不限。

不同题可同时运行；同题的 attempt 也可同时运行，但 `K` 大于 `Q` 时会分批。结果最终按输入题序和 attempt
序号排序，不依赖完成先后。请注意，开启 Judge 后，`K` 也会放大最多需要的 Judge 次数。

## 4. Canonical 工具路径

canonical 名称严格取自参考 `graph_analysis_skills/SKILL.md`、对应工具文件及真实题库所用的 perturbation
动作：retrieval 4 个、IO 3 个、theory 61 个、insight 17 个、ML 6 个，以及 `graph_task_solver`。例如：

```text
nx.density                       -> get_density
nx.common_neighbors              -> get_common_neighbors
extract_subgraph                 -> extract_n_hop_ego_subgraph
structural_*_classification      -> matching train, then matching infer
```

shell/exec 轨迹中形如 `graph_theory_tools.py get_density ...` 的调用也会解析到同一命名空间。`strict_path`
保留失败、重复、未知和多余调用；`successful_path` 只保留有成功证据的调用。二者都会记录，但论文 TSA
使用 `strict_path`，不会用过滤失败调用后的路径美化成绩。

## 5. `--no-evaluate`：只采集并保存原始 GT

`--no-evaluate` 是默认值。在此模式中 runner：

1. 只运行或回放 agent，不计算答案分数、路径分数，也不启动 Judge；
2. 先等待整个批次的所有 agent executor 完全退出；
3. 再为每道题写入 `captures/<question>.json`，其中原始 ground truth 只保存一次，并包含该题所有 attempt 的
   最终答案、reasoning/solution、原始 transcript、原始工具调用、canonical `strict_path` 与
   `successful_path`；
4. 在根 `result.json` 只放摘要、capture/attempt artifact 路径及执行成功率。

因此，采集过程中 ground truth 不会被传给仍在运行的 agent；但完成后的 capture 本身含原始 ground
truth，结果目录必须按敏感评测数据管理。

## 6. `--evaluate`：TSA、答案诊断与二值 SR

`--evaluate` 必须和 `--judge-command-json` 一起使用，并且不能与 `--validate-only` 组合。所有 agent 都结束
后才进入评估阶段；Judge 仍按题间/题内并行参数受控并发。该模式不会生成含 raw GT 的
`captures/*.json`。

### Tool Selection Accuracy

对第 `i` 个 attempt，令 `S_agent^(i)` 为 agent 的 canonical `strict_path`，`S_true^(i)` 为与之匹配的
ground-truth/accepted 工具路径。若题目声明多个非空合法路径，runner 先按 exact、单 attempt TSA、LCS 与
F1 规则选出匹配路径。官方 Tool Selection Accuracy 使用论文的 ratio-of-sums：

```text
TSA = Σ_i LCS(S_agent^(i), S_true^(i)) / Σ_i |S_true^(i)|
```

根结果中对应字段是 `tool_selection_accuracy`，并同时给出可审计的
`tool_selection_lcs_sum` 与 `tool_selection_expected_length_sum`。每个 attempt 都是一个观测，包括执行失败
产生的空路径；不能先按 ground truth 挑选“最佳” attempt。`successful_path` 的 accuracy/F1 以及 exact
pass@K 仅作辅助诊断。

### 最终答案与 Success Rate

平台保留 deterministic ground-truth exact、leaf accuracy、顶层 macro accuracy 作为诊断；官方最终答案
指标是独立 Judge 返回的二值分数。对全部 Judge 目标，`SR = Σ score / count`：

- agent 执行失败或没有 `final_answer` 时不调用 Judge，直接记 `score=0`，避免无意义的 LLM 请求；
- 其余每个 attempt 调用一次 Judge，不自动重试；
- Judge 超时、非零退出或输出非法属于评估基础设施故障，不按 0 掩盖：整次运行状态变为
  `completed_with_evaluation_errors`，退出码 `5`，正式 `success_rate` 为 `null`；
- 故障时仍输出 `judge_coverage`、完成/失败计数和 `provisional_success_rate`，后者只用于排障，不能替代 SR。
- agent 阶段发生的数据准备、物化或清理错误不属于模型答错，不计算 TSA/SR，也不调用 Judge；运行状态为
  `completed_with_data_errors`、退出码 `4`，正式指标为 `null`。若 `.pt` 在启动前的全局预检中就缺失，
  CLI 会直接返回 `data_error`/`4` receipt；其他题库或参数预检错误返回 `input_error`/`3`。

### 按图类型与问题类型统计

`summary.benchmark_matrix` 是版本化的三级交叉统计，结构为：

```text
cells[problem_group][graph_family][task_subtype]
```

- `problem_group`：`retrieval`、`theory`、`ml`、`open_qa`；后者合并 Insight 和 Prediction；
- `graph_family`：`NAG`、`TAG`、`TPG`；
- Retrieval/Theory 子类型：`Node`、`Edge`、`Graph`；
- ML 子类型：NAG/TAG 的 `NC`、`LP`，以及 TPG 的 `GC`；
- OpenQA 子类型：`Insight`、`Prediction`。

数据集分类固定为：

- NAG：DGraph-Fin、Pokec、NBA、Credit、Bail；
- TAG：Reddit、Instagram、History、Product、Arxiv、Cora；
- TPG：HIV、PCBA。

名称匹配忽略大小写及 `-`、`_`、空格，因此 `DGraph-Fin`、`dgraphfin` 和 `dgraph_fin` 指向同一分类，
但原始 `by_dataset` 键不会被改写。截图未定义的 `pubmed`、`fitness` 和自定义数据集进入 `UNKNOWN`；
`classification_complete`、`unmapped_datasets`、`unknown_task_types` 与 `unexpected_cells` 用于检查是否可以
完整填表。任何题目都会进入且只进入一个 cell，不会静默遗漏。

每个 cell 直接复用正式聚合口径：TSA 是该 cell 的 `ΣLCS/Σ|S_true|`，SR 是该 cell 的二值 Judge
成功数/目标数。多个 attempt 都是独立观测；不会先平均每题百分比，也不是 pass@K。空 cell 的 TSA/SR
为 `null` 而不是 0；数据错误或 Judge 覆盖不完整时，仅受影响 cell 的正式指标为 `null`，并保留 provisional
指标及覆盖率供审计。数值均为 0–1，小数表格展示时可再乘 100。

### Judge adapter 严格协议

Judge 和 agent 是两个独立的无 shell 子进程。`--judge-env NAME` 只把指定变量传给 Judge；不要把密钥写进
argv。Judge stdin 是唯一一个 strict JSON 对象，核心字段为：

```json
{
  "original_question": "...",
  "ground_truth": {},
  "final_answer": {},
  "rubric": "..."
}
```

其中 ground truth 顶层有 `answer` 时，传入的是该 `answer`。stdout 必须只输出一个 strict JSON 对象，
其中 `score` 只能是数值 0 或 1：

```json
{"score": 0}
```

```json
{"score": 1}
```

可选的 `reason` 字段必须是字符串，其他附加元数据字段会被接受但不会原样持久化。不能在 JSON 外追加
说明、日志、Markdown fence 或第二个 JSON；bool、字符串分数、`NaN/Infinity` 都会被拒绝。日志应写
stderr，但其内容不会复制进评估结果；结果只记录字节数、哈希、字段名和状态。

## 7. Ground truth 诊断规则（仅 `--evaluate`）

- theory/retrieval：若 ground truth 有 `answer`，只用 `answer` 评分，`subgraph` 用于数据生成/审计；
- ml：支持 bool、整数（包括 `-1`）等 scalar 标签；
- insight/prediction：递归比较以 canonical 工具名为 key 的对象；
- 数值使用 `--numeric-tolerance` 的相对/绝对容差；bool 不与整数混为一类；
- 大对象同时报告叶子 micro accuracy 和顶层工具 macro accuracy，并保存预期值的 SHA-256。

## 8. 虚拟图机器学习

默认的 `graph_analysis_skills/graph_ml_tools.py` 仅暴露六个动作：

```text
structural_node_embedding_classification_train
structural_node_embedding_classification_infer
structural_node_embedding_link_prediction_train
structural_node_embedding_link_prediction_infer
structural_graph_embedding_classification_train
structural_graph_embedding_classification_infer
```

train 只做输入 shape 校验并生成 deterministic model state；infer 使用局部 deterministic RNG，并从当前
attempt 的 broker 返回题目要求的虚拟标签。LP 同时返回与标签一致的 `0.9/0.1` 概率。ground truth 仅在
broker 内存中，agent 工作区没有答案 context 文件；broker 记录每次实际 ML 调用。不同题、同题多 attempt
并行时互不污染。

这里的隔离边界是 runner 协议而不是操作系统沙箱：agent adapter 被视为可信进程。若要评测不可信或可能
主动读取题库的 agent，应在独立 UID、容器或 mount namespace 中运行，只挂载 attempt workspace、数据集
和工具；不要向该运行环境挂载原始 questions 目录或 runner 的结果目录。`--evaluate` 的评分 artifact
不保存原始 ground truth；默认 `--no-evaluate` 则会在全部 agent 退出后把原始 ground truth 写进每题
capture，必须隔离该结果目录。

## 9. 在 Linux 服务器上通过 `.sh` 启动

仓库不包含服务器地址、登录信息、私钥、API URL 或 API key。请在目标 shell 中自行配置路径和凭据：

```bash
conda activate vlm
cd /path/to/iGraph-benchmark
export IGRAPH_QUESTIONS_ROOT=/path/to/questions-root
export IGRAPH_DATASET_ROOT=/path/to/datasets
export IGRAPH_RESULTS_ROOT=./outputs

# 默认采集模式：不会调用 Judge
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --datasets cora \
  --categories theory retrieval ml insight prediction \
  --questions-per-type 20 \
  --shuffle --seed 42 \
  --task-parallelism 4 \
  --attempts-per-question 2 \
  --intra-task-parallelism 2 \
  --agent-command-json '["python","/path/to/agent_adapter.py"]' \
  --agent-env OPENAI_API_KEY \
  --no-evaluate \
  --output-dir "$IGRAPH_RESULTS_ROOT"
```

需要正式自动评估时才加入 Judge：

```bash
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --datasets cora \
  --categories theory retrieval ml insight prediction \
  --questions-per-type 20 \
  --shuffle --seed 42 \
  --task-parallelism 4 \
  --attempts-per-question 1 \
  --intra-task-parallelism 1 \
  --agent-command-json '["python","/path/to/agent_adapter.py"]' \
  --agent-env OPENAI_API_KEY \
  --evaluate \
  --judge-command-json '["python","/path/to/judge_adapter.py"]' \
  --judge-env OPENAI_API_KEY \
  --output-dir "$IGRAPH_RESULTS_ROOT"
```

上述 `--agent-command-json` 与 `--judge-command-json` 都是 JSON argv 数组，runner 始终以 `shell=False`
启动。若当前 shell 未激活 `vlm`，`run_benchmark.sh` 也会按 `BENCHMARK_CONDA_ENV=vlm` 尝试激活。

## 10. 推荐验收顺序（不调用 LLM）

下面的 unittest 命令仅适用于包含 `tests/` 的源仓库；白名单构建的发布包刻意不包含测试代码。发布包仍可
执行后面的 `--validate-only` 命令。

```bash
conda activate vlm
bash -n run_benchmark.sh
python -m unittest \
  tests.test_questions \
  tests.test_tool_names \
  tests.test_virtual_graph_ml \
  tests.test_virtual_ml_broker \
  tests.test_agent_protocol \
  tests.test_question_scoring \
  tests.test_parallel_runner \
  tests.test_question_cli \
  tests.test_dataset_routing \
  tests.test_evaluation_modes \
  tests.test_judge_and_masked_dataset
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --datasets cora \
  --categories theory retrieval ml insight prediction \
  --questions-per-type 20 \
  --shuffle --seed 42 \
  --validate-only
```

`--validate-only` 与上述 unittest 都不会启动 Judge。真实 agent/Judge 测评应留到代码、题库与数据预检
全部通过之后；均衡测评先从 `--questions-per-type 1`、`--attempts-per-question 1` 开始。Cora 的 agent
调用数为 `10 × N × attempts-per-question`，开启 Judge 时其调用上限与 agent attempt 数相同。若退出均衡
模式，才改用较小的 `--question-count`。
