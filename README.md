# iGraph Headless Benchmark

这是一个面向图任务的无头 benchmark 平台：没有前端、登录、数据库或常驻 Web 服务。推荐入口只有
`run_benchmark.sh`。当前主要能力是从标准 `questions.json` 题库并行运行 agent、保存完整解题/工具
轨迹，并分别评测最终答案和工具调用路径；原有 HIV/PCBA 图分类训练 benchmark 作为兼容子命令保留。

## 运行环境

正式运行面向 Linux Conda 环境。脚本默认自动激活 `vlm`，也可以显式覆盖：

```bash
BENCHMARK_CONDA_ENV=vlm bash run_benchmark.sh --help
```

如果调用方已指定可执行的解释器，可设置 `PYTHON_BIN=/path/to/python`。启动脚本不会安装依赖、
不会读取 `.env`，也不会自行调用任何 LLM API。

题库、数据集和输出目录都由调用方提供，不内置服务器地址或个人目录：

```bash
export IGRAPH_QUESTIONS_ROOT=/path/to/questions-root
export IGRAPH_DATASET_ROOT=/path/to/datasets
export IGRAPH_RESULTS_ROOT=./outputs
```

使用可选的 Codex/provider 包装脚本时，再在仓库外准备 provider profile，并设置：

```bash
export IGRAPH_CODEX_HARNESS_ROOT=/path/to/codex-harness
export IGRAPH_PROVIDER_PROFILE=/path/to/provider-profile.sh
# 可选：限制 profile 中允许使用的 HTTPS 主机名
export IGRAPH_PROVIDER_ALLOWED_HOSTS=provider.example
```

provider profile 和上述变量的实际值不得提交到仓库。也可以改用对应的命令行参数。

## 最安全的首次检查

下面的命令只检查题目结构、canonical 工具名称和 `.pt` 文件是否存在，不启动 agent：

```bash
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --categories theory retrieval ml insight prediction \
  --question-count 10 \
  --task-parallelism 4 \
  --validate-only
```

`--question-count` 默认是 `1`，用于避免误耗 API token；传 `0` 才表示全部题目。

需要按最终统计细分类严格等量抽样时，使用 `--questions-per-type N`。该参数与
`summary.benchmark_matrix` 使用同一套 `problem_group/graph_family/task_subtype` 分类；任一正式 cell
少于 N 题、落入 `UNKNOWN` 或属于非法组合时，会在启动 agent 前报错，绝不静默少抽。Cora 只有 TAG，
五个源类别合计 10 个细分类，因此 `--datasets cora --questions-per-type 20` 精确选择 200 题。Cora 的
Retrieval/Edge 只有 28 题，所以 N 最大为 28。均衡模式不能再设置正数 `--question-count` 或
`--questions-per-file`；`--shuffle --seed 42` 可做可复现的分层随机抽样。
若显式指定数据集，均衡模式还会要求每个已选类别都存在对应的 `questions.json`，避免缺少整个类别时
静默退化成少于 10 个细分类。多个同属一种 graph family 的数据集会按最终 matrix cell 合并抽样，
而不是每个数据集各抽 N 题。

```bash
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --datasets cora \
  --categories theory retrieval ml insight prediction \
  --questions-per-type 20 \
  --shuffle --seed 42 \
  --validate-only
```

## 并行采集（默认、不评分）

```bash
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --question-count 100 \
  --task-parallelism 8 \
  --attempts-per-question 3 \
  --intra-task-parallelism 2 \
  --agent-command-json '["python","/path/to/agent_adapter.py"]' \
  --agent-env OPENAI_API_KEY \
  --no-evaluate \
  --output-dir "$IGRAPH_RESULTS_ROOT"
```

- `--task-parallelism`：同时处理多少道不同题目；
- `--attempts-per-question`：每题独立运行多少次；
- `--intra-task-parallelism`：同一道题最多并行多少个 attempt，同时会通过 task envelope 和
  `IGRAPH_INTRA_TASK_PARALLELISM` 传给 agent adapter；
- 最大并发 agent 进程数为
  `task-parallelism × min(intra-task-parallelism, attempts-per-question)`。

`--no-evaluate` 是默认值。该模式只运行 agent，不调用 Judge、也不计算答案或路径分数。整个批次的所有
agent 进程退出后，runner 才会在 `captures/` 下为每道题写一个自包含 JSON，其中包括原题、原始
ground truth、每个 attempt 的最终答案、reasoning/solution、transcript、原始工具调用，以及 canonical
化后的 `strict_path` 和 `successful_path`。这样采集阶段不会把 ground truth 暴露给仍在运行的 agent。

agent 命令必须是 JSON argv 数组，runner 使用 `shell=False` 启动，并把单题 envelope 作为 JSON 写到
stdin。凭据应由 agent 自己从明确允许的环境变量读取，不要放进命令行。输出协议和题库规范详见
[Question Benchmark 指南](docs/QUESTION_BENCHMARK.md)。

runner 默认只继承运行所需的基础环境。如 agent 需要 API 凭据，按变量名显式放行，值不会写入参数或
运行配置：`--agent-env OPENAI_API_KEY`（多个变量可重复传入）。

## 开启自动评估

只有显式传 `--evaluate` 才会评估；此时必须同时提供独立的二值 Judge adapter：

```bash
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --categories theory retrieval ml insight prediction \
  --question-count 20 \
  --task-parallelism 4 \
  --attempts-per-question 1 \
  --intra-task-parallelism 1 \
  --agent-command-json '["python","/path/to/agent_adapter.py"]' \
  --evaluate \
  --judge-command-json '["python","/path/to/judge_adapter.py"]' \
  --judge-env OPENAI_API_KEY
```

工具调用路径采用论文中的 Tool Selection Accuracy（TSA）。对每个 attempt，将 agent 的 canonical
`strict_path` 与选中的 ground-truth/accepted 路径计算最长公共子序列（LCS），然后在整个集合上计算
`TSA = Σ LCS(S_agent, S_true) / Σ |S_true|`。`strict_path` 会保留失败、重复、未知和多余调用；
`successful_path` 仍保存作诊断，但不是 TSA 的主路径。

最终答案的主指标是 Judge 给出的严格二值 Success Rate（SR）。Judge stdin 是一个 JSON 对象，核心字段为
`original_question`、`ground_truth`、`final_answer`、`rubric`；stdout 必须只包含一个 strict JSON 对象，
其中 `score` 必须是数值 `0` 或 `1`，可选 `reason` 必须是字符串。不能在 JSON 外夹带日志、Markdown 或
第二个对象，`NaN/Infinity`、bool、字符串分数也会被拒绝。Judge 超时、非零退出或输出非法时不会自动重试；该次运行标记为
`completed_with_evaluation_errors`，`success_rate` 为 `null`，进程退出码为 `5`，并同时报告覆盖率和
`provisional_success_rate` 供排障。agent 本身未成功或没有最终答案时不会调用 Judge，直接计 0，以免浪费
LLM token。

## 无 API 回放采集

已有 agent 输出时可以在不启动 agent/Judge 的情况下重新生成采集结果：

```bash
bash run_benchmark.sh questions \
  --questions-root "$IGRAPH_QUESTIONS_ROOT" \
  --dataset-root "$IGRAPH_DATASET_ROOT" \
  --question-count 100 \
  --task-parallelism 8 \
  --attempts-per-question 2 \
  --intra-task-parallelism 2 \
  --replay-file /path/to/responses.jsonl \
  --no-evaluate
```

若回放时加 `--evaluate`，仍必须提供 `--judge-command-json`，且会对有最终答案的 attempt 调用 Judge。
当回放输入是平台生成的根 `result.json`，或是带有 `trajectory.calls` 的 replay record 时，runner 会优先
复用已经捕获的 canonical trajectory，而不会再从 agent transcript 合并或重建工具路径。这样可保留虚拟
图机器学习 broker 记录的权威调用，并避免同一调用在 transcript 与捕获路径之间重复计数。旧版仅包含
agent response 的 JSON/JSONL 回放格式保持兼容。

`--recover-cleanup-failures` 只能和 `--replay-file` 一起显式使用。它只恢复同时满足以下条件的旧记录：已有
非空最终答案、已有非空 transcript、唯一错误严格为 `ContainerCleanupError`。旧版将 error mapping 保存为
`type=dict` 时，runner 使用安全的 Python literal 解析确认原始错误类型，不会按日志子串猜测。恢复后的
answer 和捕获路径可以正常评分；原错误会保存在 `replay_recovery` 中并追加结构化 warning，因此报告仍以
`completed_with_errors`/退出码 `5` 提醒清理异常。

对已经执行过部分 Judge 的评估结果，可在 replay evaluation 中显式加入
`--reuse-successful-judge-results`。平台只复用 `status=success` 且 `score` 是非 bool 整数 `0` 或 `1` 的
记录；失败、缺失、浮点、字符串或其他非法分数都会重新调用配置的 Judge。若 agent payload 来自 attempt
artifact，Judge 也只从同一个 artifact 的 `evaluation.judge` 读取，不会和根摘要交叉配对。复用项仍以
`source=llm_as_a_judge` 计入 SR，并由 `provenance.result_origin=replay` 标记来源；汇总中的
`judge_reused_count` 给出本次节省的 Judge 调用数，`judge_invocation_count` 给出本次实际补评调用数。

服务器包装脚本也支持相同开关，例如：

```bash
bash scripts/run_zhizengzeng_benchmark.sh \
  --output-dir /path/to/replay-output \
  --cpuset-cpus 32-39,96-103 \
  --questions-per-type 20 \
  --skills-dir /path/to/graph_analysis_skills \
  --categories prediction \
  --replay-file /path/to/previous/result.json \
  --recover-cleanup-failures \
  --reuse-successful-judge-results \
  --evaluate -- hiv pcba
```

该包装脚本默认让 Agent 使用 `glm-5-turbo`，让 Judge 使用 `gpt-5-mini`。可用
`--agent-model` 和 `--judge-model` 分别覆盖；二者继续共享 `--provider-profile` 中的 API URL
和 key。旧版 `--model NAME` 仍兼容：未提供角色专用参数时，它会同时设置 Agent 和 Judge；若同时
提供，角色专用参数优先，且不受参数先后顺序影响。例如：

```bash
bash scripts/run_zhizengzeng_benchmark.sh \
  --output-dir /path/to/output \
  --cpuset-cpus 32-39,96-103 \
  --questions-per-type 20 \
  --skills-dir /path/to/graph_analysis_skills \
  --agent-model glm-5-turbo \
  --judge-model gpt-5-mini \
  --categories insight,prediction \
  --evaluate -- NAG TAG TPG
```

包装脚本的默认 backend 仍是 `codex`，以保持已有命令兼容。测试项目自研 iGraph Agent 时必须显式
选择 `--agent-backend igraph`，否则结果不能标为 iGraph Agent：

```bash
bash scripts/run_zhizengzeng_benchmark.sh \
  --agent-backend igraph \
  --igraph-runtime-root /path/to/official/igraph-main \
  --skills-dir /path/to/graph_analysis_skills \
  --output-dir /path/to/output \
  --cpuset-cpus 0-7,64-71 \
  --questions-per-type 10 \
  --agent-model gpt-5.6-sol \
  --judge-model deepseek-v4.1-flash \
  --categories insight,prediction \
  --provider-profile /path/outside/repository/provider-profile.sh \
  --questions-root /path/to/questions \
  --dataset-root /path/to/dataset \
  --evaluate -- cora reddit
```

该模式直接导入完整源码树中的 `agent_runtime.agent.igraph.IGraph`，不会导入 Codex harness。每个
attempt 在首次对话前把 Agent 的 MCP client 替换为 attempt-local canonical skill backend；该 backend
直接执行 `--skills-dir` 中的 `graph_analysis_skills` 脚本，并默认绑定 runner 物化的隔离
`dataset_path`。六个 structural ML 动作仍通过 runner 的 attempt-scoped broker 执行和记录；
旧的 label-injection ML 脚本会被 fail closed 拒绝。provider URL/key 只从仓库外 profile 读取，
不传入工具子进程，adapter stdout 始终只含一条 benchmark JSON。

`gpt-5.6-sol` 需要外部 iGraph runtime 提供对应的模型注册和 Responses API 支持；benchmark
adapter 不会修改产品 runtime。当前 Responses 路径要求文本工具协议，adapter 会显式设置
`LLM_TOOL_PROTOCOL=text`。其他模型的传输方式仍由所选 runtime 决定。

## ML / prediction 数据输入

这两个 category 不直接把 `dataset/<dataset>.pt` 当作题目数据：

- `ml/<dataset>/questions.json` 使用同目录的 `pure_ml_bench_masked_data.pt`；
- `prediction/<dataset>/questions.json` 使用同目录的
  `intermediate_ground_truth_masked_data.pt`；
- `<dataset-root>/<dataset>.pt` 对这两类题只作为基础图。

masked 条目按题目加载时保留的原始 `source_index` 选取，不受筛选、shuffle 或并发完成顺序影响。命令模式
按参考流程注入相应字段：NC 替换标签、LP 替换边、GC 替换目标图标签；GC 容器会更新
`graphs[context.graph_idx]`，不会误改顶层对象。同题的并发 attempt 各自获得独立临时 `.pt`，避免一个 agent
的写入污染另一个 attempt；每个 agent 退出后立即删除自己的临时文件。

masked 文件使用 `torch.load(..., mmap=True, weights_only=True)`，并按路径做进程级缓存，避免每道题重复
映射超大 PT；缓存的是 mmap-backed 整体映射，不能理解为真正的单 key 文件读取。题目批次结束后释放该
缓存。每题物化仍需保存基础图，因此应为超大数据预留磁盘空间。

数据物化或 runner 基础设施失败不会被当成模型答错，也不会触发 Judge；运行状态为
`completed_with_data_errors`、退出码为 `4`，正式 TSA/SR 为 `null`。

## 结果与评分

stdout 只打印一行 compact JSON receipt；完整运行索引写入
`outputs/question-benchmarks/questions-<UTC>-<id>/result.json`（或 `--output-dir` 下对应运行目录）。每个
attempt 另有独立 JSON，包含：

- agent 最终答案、reasoning/solution、原始 transcript；完整 stdout/stderr 分别保存在 attempt workspace
  的日志文件中，结果 JSON 的展示字段仅保留最后 1,000,000 bytes，并记录大小与截断标志；
- 原始工具调用及 canonical 化后的严格路径、成功路径。

在默认采集模式，每题 `captures/*.json` 保存一次完整轨迹和原始 ground truth，不含任何分数。开启评估
后不生成这类 raw-GT capture；attempt artifact 增加 deterministic ground-truth 诊断、工具路径明细和
Judge 状态，根结果报告 overall、按 category、按 dataset 聚合的 TSA、SR、计数与覆盖率。根
`result.json` 只保留 attempt 摘要和 artifact 路径，完整轨迹不重复嵌入。

评估结果还提供 `summary.benchmark_matrix.cells`，可直接按截图中的三级维度取数：问题组
（Retrieval/Theory/ML/OpenQA）→ 图类型（NAG/TAG/TPG）→ 子类型
（Node/Edge/Graph、NC/LP/GC、Insight/Prediction）。每个单元都重新累计 LCS 分子、标准路径长度分母
和 Judge 计数，不会平均已有百分比。截图未定义的 `pubmed`、`fitness` 或自定义数据集保留在
`UNKNOWN`，并列入 `unmapped_datasets`，不会静默漏题或猜测分类。

图机器学习默认启用与参考实现一致的虚拟 train/infer 工具，不做真实优化或 LLM 调用。每个 attempt
使用隔离的工具 broker，ground truth 只保留在 runner 内存，不写入 agent 工作区；broker 同时记录实际
ML 调用。`--no-virtual-graph-ml` 只关闭这个 broker 和内置虚拟工具注入；若要真实训练，agent adapter
必须自行提供真实 ML 工具。

agent adapter 默认按可信进程处理，并不是文件系统安全沙箱。评测不可信 agent 时，应另外用独立 UID、
容器或 mount namespace 隔离原始 questions 与 runner 结果目录。特别是默认 capture 最终会包含原始
ground truth，应限制结果目录权限。

## 独立发布包

仓库历史目录中仍可能存在旧产品代码。正式 benchmark 包通过白名单构建，明确排除 frontend、backend、
登录/会话服务、AgentOS、MCP 和部署文件：

```bash
python scripts/build_benchmark_bundle.py /path/to/igraph-benchmark
```

白名单位于 `benchmark-package.txt`，目标目录必须不存在。

## 兼容的图分类 benchmark

```bash
bash run_benchmark.sh graph-classification hiv /path/to/hiv.pt \
  --models GCN GAT SAGE --epochs 1 --device cpu
```

旧命令 `bash run_benchmark.sh hiv /path/to/hiv.pt ...` 仍可用。详细约束见
[图分类 Benchmark 指南](docs/GRAPH_CLASSIFICATION_BENCHMARK.md)。
