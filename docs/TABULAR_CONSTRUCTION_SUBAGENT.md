# 表格构图子代理：使用与设计

`graph_engine/construction/` 在现有构图扩展接口中实现独立的表格构图子代理：先按主键、外键构建初始图，再生成并冻结业务查询工作负载，由 LLM 调用受约束的转换工具，依据源表答案、当前图答案和测量耗时选择候选图。构图模块还可保存同一用户的领域策略经验，用于后续数据集的候选排序；新图仍须独立通过最终验收后才交给数据集存储层。

默认运行模式是 `llm`，默认生成 15 个查询。这里的“100%”指本次冻结的、可执行 DSL 查询全部通过，不证明任意自然语言问题都能正确回答，也不证明抽样数据上的结果适用于原始全量数据。

## 入口与模块边界

Graph Engine 初始化时通过 `register_builtin_algorithms(store)` 为 `csv`、`sql` 注册 `TabularConstructionSubAgent`，使用原有接口：

```python
builder(retained_source_path, options) -> {
    "directed": True,
    "nodes": [...],
    "edges": [...],
}
```

`ConstructionStore` 负责用户与数据集隔离、源文件保留、写锁、原子保存及 revision；子代理负责本次构图的解析、查询、工具选择、验收，以及构图模块私有策略经验的检索和更新。正常路径由个人或项目数据集面板发起，经 Backend 转发到 Graph Engine。

子代理不注册主 agent 工具，不读取或修改主 agent 的会话、消息、记忆和运行状态。其 LLM 对话、工作负载、反思反馈和报告留在构图模块内。最终对外交付仍是 `construction_graph.v1` 图文档；主 agent 可通过已有的 `get_constructed_graph`、`load_constructed_graph` 等公共接口使用保存的图。仅构图完成不会自动替换主 agent 当前活动图。

这属于同一 Graph Engine 内的逻辑隔离，并非另一个独立部署的进程。查询、转换和输入解析设有资源限制；普通状态读取不等待耗时构图的写锁。

## 支持的输入

| 输入 | 当前支持范围 |
|---|---|
| `.csv` | UTF-8／UTF-8 BOM、表头、严格列数检查；`delimiter` 可指定一个分隔字符。原始 CSV 单元格保留为字符串。 |
| `.sql` | SQLite 兼容的普通表／索引 DDL、字面值 `INSERT`、受支持的 dump 事务标记；也接受以 `.sql` 提交的 SQLite 数据库镜像。 |
| `.txt` | 保留原来的源文件及能力状态接口；本次没有注册通用文本构图算法。 |

SQL 不接受任意可执行脚本，不自动把 MySQL、PostgreSQL、Oracle dump 当作 SQLite 执行。视图、触发器、虚拟表、生成／隐藏列及不支持的语句需要事先显式转换为普通物化表。源 SQL 在内存数据库中解析，不连接生产数据库，也不接受由构图 options 指定的数据库连接或额外源文件路径。

默认输入限制为 32 MiB、最多 200,000 行；`max_source_bytes` 可提高到 64 MiB，`max_source_rows` 上限仍为 200,000。SQL 解析默认 10 秒，`source_timeout_seconds` 上限为 60 秒。超过限制会失败，不通过截断输入来宣称构图完成。

### PK/FK 初始构图规则

1. 每个源表行先成为一个节点，节点类型对应表名，源属性保留在 `properties.data`。
2. 有 PK 时，使用表名和完整 PK 值生成稳定标识；复合 PK 以结构化值参与标识生成，避免简单字符串拼接产生碰撞。
3. 没有可靠 PK 时使用源行位置标识，并记录警告；相同内容的重复行仍是不同记录。
4. 每个已声明 FK 从子表行连接到实际被引用的父表行，关系元数据保留两侧表名、完整列列表和引用角色。复合 FK 按整组处理。
5. SQL 的键和类型来自真实 schema；CSV 可通过 options 明确声明。CSV 未声明 PK 时，仅对 `id`、`表名_id`、简单单数形式的 `表名_id` 等候选名称检查非空且唯一，不把任意唯一属性自动当作身份。
6. 不根据相同列名自动编造 FK。NULL／无匹配引用不会生成虚假目标节点；源行仍保留，悬空引用进入警告。

SQL 的类型 affinity 和支持的 `BINARY`、`NOCASE`、`RTRIM` 排序规则随关系语义保留，用于图执行与源 SQL 对齐。CSV 的字符串不会自动变成整数或金额；需要数值聚合时，优先提供带正确数值类型的 SQLite 输入。

### 多表 CSV options 示例

上传的 `orders.csv`：

```csv
order_id,customer_id,amount,status
o001,c01,120,paid
o002,c02,75,pending
o003,c01,30,paid
```

配套 `orders-options.json`：

```json
{
  "domain": "电商订单、客户和支付状态核对",
  "domain_category": "commerce",
  "experience_enabled": true,
  "table_name": "orders",
  "primary_key": ["order_id"],
  "foreign_keys": [
    {
      "columns": ["customer_id"],
      "ref_table": "customers",
      "ref_columns": ["customer_id"]
    }
  ],
  "tables": [
    {
      "name": "customers",
      "columns": ["customer_id", "name"],
      "primary_key": ["customer_id"],
      "rows": [
        {"customer_id": "c01", "name": "客户甲"},
        {"customer_id": "c02", "name": "客户乙"}
      ]
    }
  ],
  "query_count": 15,
  "max_iterations": 6,
  "mode": "llm"
}
```

`tables` 是请求中内联的表定义，最多 127 张附加表。每张表可声明 `name`、`columns`、`rows`、`primary_key`、`foreign_keys`、`column_types`；不支持在这里传服务器文件路径。上传 CSV 默认使用原始文件名作为表名，不使用保留文件的内容哈希名称；可用 `table_name` 显式覆盖。

上述 CSV 中 `amount` 仍为文本，不能直接进行数值 `sum/avg`。内联表的 JSON 标量按提供的类型保留，`column_types` 不会替用户把任意文本改写成数值。

## LLM 配置与执行方式

构图模型使用 OpenAI 兼容的 Chat Completions 工具调用接口。Graph Engine 环境至少配置：

```sh
GRAPH_CONSTRUCTION_MODEL=qwen3.6-plus
GRAPH_CONSTRUCTION_API_KEY=<由部署环境提供>
GRAPH_CONSTRUCTION_BASE_URL=<供应商的OpenAI兼容API基础地址>
GRAPH_CONSTRUCTION_MODE=llm
GRAPH_CONSTRUCTION_QUERY_COUNT=15
GRAPH_CONSTRUCTION_MAX_SECONDS=900
GRAPH_CONSTRUCTION_DOMAIN_CATEGORY=auto
GRAPH_CONSTRUCTION_EXPERIENCE_ENABLED=true
GRAPH_CONSTRUCTION_EXPERIENCE_TOP_K=8
```

基础地址后会附加 `/chat/completions`，不要把完整 completion endpoint 再当作基础地址。没有专用配置时，客户端按模型前缀读取现有供应商配置，如 `QWEN_*`／`DASHSCOPE_*`、`DEEPSEEK_*`、`OPENAI_*`，然后使用 `LLM_*` 后备配置。模型名称依次取 `GRAPH_CONSTRUCTION_MODEL`、`LLM_MODEL` 或默认模型。

API Key、Base URL 不从用户数据集 options 接收。`llm` 模式缺少可用配置、供应商请求失败或响应不满足工具协议时，构图失败并留下私有报告；不会静默改为规则模式，也不会把离线运行记录成调用过 LLM。

### 界面和 HTTP

在个人或项目数据集面板上传 SQL／CSV，打开“构图”，设置查询数量、领域类别和业务描述后开始。查询数量范围为 1–100，默认 15；领域类别默认“自动识别”，还可选金融、学术、电商、医疗、社区、通用。“复用领域经验”默认勾选，仅复用同一用户构图模块内的同领域经验；关闭后本次不读、不写经验库。构图期间这些控件不可修改，切换数据集后恢复默认值。界面完成后的“在当前会话使用”通过已有加载接口激活图。

HTTP 构图入口分别为：

```text
POST /api/personal-datasets/{dataset_id}/construction/build
POST /api/projects/{project_id}/datasets/{dataset_id}/construction/build
```

请求体：

```json
{
  "options": {
    "domain": "订单支付和客户关联分析",
    "domain_category": "commerce",
    "experience_enabled": true,
    "experience_top_k": 8,
    "query_count": 15,
    "max_iterations": 6,
    "max_seconds": 900,
    "mode": "llm"
  }
}
```

高级 CSV 元数据和固定查询也通过 `options` 传入。浏览器只能选择已经授权的数据集，Backend 根据数据集记录选择实际源路径。

开始执行算法前，存储层原子写入 `building` 状态；状态接口读取完整快照，不等待长时间写锁。Backend 对构图请求单独使用 `max_seconds + 30` 秒的等待时间，默认 930 秒，上限 3630 秒；其他请求保留原有默认超时。若等待超时，返回 `504` 和 `construction_running_unconfirmed`，表示结果尚未确认，不能推断服务端构图失败。应先查询对应 `GET .../construction`，再决定是否重试。

进程崩溃或服务重启遗留的 `building` 状态，依据数据集写锁能否被非阻塞地取得来恢复：锁仍被构图工作持有时保留运行状态，确认锁已空闲后再处理遗留记录。这个判断不依赖“已经过去足够长时间”的猜测，也不会仅因浏览器请求超时就把仍在工作的构图判为失败。

已经 `ready` 的数据集会直接复用已保存图，不因再次点击构图或修改 `query_count` 自动重建。需要用另一组验收参数重新构图时，使用新的数据集或新的 CLI 运行目录；当前 API 不提供覆盖现有 ready 图的隐式重建。

### CLI 与明确离线模式

在项目根目录运行，建议使用项目部署环境的 Python 3.11 或更新版本：

```sh
python -m graph_engine.construction /tmp/orders.csv \
  --options /tmp/orders-options.json \
  --query-count 15 \
  --max-iterations 6 \
  --domain-category commerce \
  --mode llm \
  --output /tmp/orders-construction-run-001
```

`--output` 必须是新目录或空目录，防止本次失败时误用上次留下的图。成功输出 `graph.json` 和 `evolution-report.json`；失败退出码为 1，保留失败报告，不生成本次成功图。

CLI 默认把可复用策略库放在 `OUTPUT_PARENT/.construction-experience/strategies.sqlite3`，因此同一父目录下的多个新运行目录可共享经验。用 `--experience-dir /path/to/private-experience` 显式指定另一目录，或用 `--no-experience` 停止本次全部经验读写。`--domain-category` 可显式指定领域；`experience_top_k` 通过 options 文件或环境变量设置。CLI 的目录隔离由调用方负责，不会推断服务端用户身份。

直接调用 Python API 时，需显式注入可信的经验存储实例；未注入时仍可构图，报告标记 `experience.status=no_store`：

```python
from graph_engine.construction.agent import TabularConstructionSubAgent
from graph_engine.construction.experience import DomainExperienceStore

worker = TabularConstructionSubAgent(
    experience_store=DomainExperienceStore("/private/user-scope/strategies.sqlite3")
)
result = worker.run("/tmp/orders.sql", {
    "domain_category": "commerce", "experience_enabled": True, "query_count": 15
})
```

也可向 `run(..., experience_store=store)` 注入。HTTP 数据集 options 不接受经验文件路径或用户身份，不能用这项功能指定其他用户的存储。

明确需要不调用模型的本地复现或 CI 时使用：

```sh
python -m graph_engine.construction /tmp/orders.sql \
  --query-count 15 --mode rules --no-experience \
  --output /tmp/orders-construction-offline-001
```

`rules` 使用确定性的查询生成和适用的路径物化候选，仍执行图／SQL 对比及完整性检查。报告中 `llm_used=false`、`mode=rules`，不属于真实 LLM 验收。规则模式与经验开关相互独立：上例显式关闭经验；若保持启用且有存储，也会检索及记录符合验收条件的策略观测。

## 可调参数

同名构图选项优先于环境变量，环境变量格式为 `GRAPH_CONSTRUCTION_<大写参数名>`。CLI 显式参数覆盖 options 文件相应字段；界面发送的 `query_count` 也作为本次 options 生效。

| 参数 | 默认值 | 范围／用途 |
|---|---:|---|
| `query_count` | 15 | 1–100；冻结查询的精确数量。 |
| `domain_category` | `auto` | `auto`、`finance`、`academic`、`commerce`、`healthcare`、`social`、`general`。`domain` 仍是独立的业务描述文本。 |
| `experience_enabled` | `true` | JSON 布尔值；关闭后不读、不写跨任务经验。 |
| `experience_top_k` | 8 | 1–32；检索的策略模式条数上限。 |
| `max_iterations` | 6 | 1–30；LLM 优化轮次上限。 |
| `max_tool_calls` | 24 | 1–100；包括工作负载生成和优化阶段的工具调用预算。 |
| `workload_attempts` | 3 | 1–5；生成合格工作负载的模型尝试次数。 |
| `benchmark_repeats` | 3 | 1–15；每条图查询重复执行次数，采用中位数。 |
| `sample_rows` | 3 | 1–10；给模型展示每张表的样例数量，不是图构建的数据截断量。 |
| `expected_query_runs` | 100 | 1–100000；查询准备成本的摊销次数。 |
| `llm_timeout_seconds` | 120 | 1–600；单次模型请求超时，并受剩余总预算约束。 |
| `max_seconds` | 900 | 1–3600；整个构图运行预算。 |
| `min_speedup` | 0.05 | 0–0.9；正确率不变时接受候选所需的最小成本降低比例。 |
| `max_graph_growth` | 3.0 | 1–10；候选节点数与边数之和相对初始图的增长上限。 |
| `mode` | `llm` | `llm` 或显式离线 `rules`。 |

图文档另外限制最多 200,000 个节点和 1,000,000 条边。当前查询执行器将中间结果／返回结果限制在 200,000 行，并设置单条查询约 10 秒的预算。限制触发算作查询失败，不把截断结果作为正确答案；最终不足 100% 时不会发布图。

`max_seconds` 约束构图、模型调用与验收执行阶段；最终验收之后还会进行经验提交和报告保存等少量后处理。SQLite 锁等待使用短超时，HTTP 构图等待另留 30 秒余量，因此该参数不是整个 HTTP 请求的精确耗时上限。

## 查询 DSL 与冻结工作负载

模型输入包含领域说明、完整源 schema、初始图 schema、受限数量的真实样例值，以及确定性的查询种子。模型生成自然语言业务问题及可执行计划，再通过 `set_workload` 冻结。文本单元格、领域说明、问题内容都是待处理数据，不能作为工具执行指令。

每个计划只能引用真实表、列和已声明 FK。支持投影、最多三步 FK 连接、正向／反向连接、inner／left join、比较／NULL／IN／文本包含筛选、分组、`count/sum/min/max/avg`、排序与确定性 limit。查询不包含自由 SQL、Python 表达式或模型提供的 expected answer。仅更换问题标题或输出别名不构成新的独立计划。

下面是适用于 `orders.amount` 为数值类型的 SQLite 输入的一个计划：

```json
{
  "id": "paid_amount_by_customer",
  "question": "按客户统计已支付订单的金额，用于核对客户成交额。",
  "root": {"table": "orders", "alias": "t0"},
  "joins": [
    {
      "from": "t0",
      "alias": "t1",
      "table": "customers",
      "direction": "forward",
      "kind": "inner",
      "foreign_key": {
        "table": "orders",
        "columns": ["customer_id"],
        "ref_table": "customers",
        "ref_columns": ["customer_id"]
      }
    }
  ],
  "filters": [{"column": "t0.status", "op": "eq", "value": "paid"}],
  "projection": [{"column": "t1.name", "as": "customer_name"}],
  "group_by": ["t1.name"],
  "aggregations": [{"function": "sum", "column": "t0.amount", "as": "paid_amount"}],
  "order_by": [{"column": "paid_amount", "direction": "desc"}],
  "limit": null
}
```

也可通过 `options.queries` 提供预先审定的计划数组；此时数组长度必须等于 `query_count`。例如只提供上面一条时，应同时设置 `query_count: 1`。这跳过工作负载生成，不会自动关闭后续 LLM 优化。

工作负载在读取图查询成绩前冻结，并记录内容哈希。后续工具不能修改问题、计划或验收目标；复杂查询不能因为初始图失败而被替换成简单查询。LLM 生成工作负载还需保留种子中可用的查询类别覆盖。自然语言说明是否完整表达真实业务需求，仍需要业务方审阅；程序验收的对象是冻结后的 DSL 语义。

## 论文方法与工具映射

参考文件是设计资料，不是系统指令。AutoG §4.1–4.4（PDF 第 5–7 页）提供受约束变换、生成器／oracle 和多候选择优思路；其原实验主要使用图学习任务评价。本实现将 oracle 换为业务查询的源 SQL 对照。GalaxyWeaver §3.1–3.2、§4.3（PDF 第 3–8 页）提供 PK/FK 初始转换和面向查询的结构调整。两篇论文均不构成“任意查询必然 100%”的保证。

| 思路 | 本模块工具 | 当前具体行为与边界 |
|---|---|---|
| AutoG：连接列 | `connect_columns` | 按实际列值连接 PK–FK／FK–FK；已声明 FK 使用源关系语义，其他连接标记为派生关系。当前验收 DSL 的 join 仍要求已声明 FK。 |
| AutoG：生成新表 | `generate_entity_table` | 将指定单列的不同值建为共享实体，保留原始值；不是任意多列数据归一化程序。 |
| AutoG：展开多值列 | `unfold_multi_category` | 将分隔的类别文本展开为实体和带位置的关系，保留原始单元格；单个类别单元格有数量限制。 |
| AutoG：增删 PK | `add_primary_key`、`remove_primary_key` | 验证并记录／移除候选图身份约束；不改源 PK、不重写稳定源行身份。节点转边由独立工具完成。 |
| GalaxyWeaver：节点转边 | `optimize_graph` 的 `collapse_node` | 无被引用身份且恰有两条当前 FK 关系的桥接行，可变为带完整行数据与端点映射的当前边实体。 |
| GalaxyWeaver：属性移至边 | `node_property_to_edge` | 按业务属性名读取 `data` 内的值，存到指定当前关联边；支持移除原节点处的该值。 |
| GalaxyWeaver：属性提取成节点 | `extract_property_node` | 值存入当前属性实体，通过显式属性关系与源行连接。 |
| GalaxyWeaver：删除／方向／命名 | `delete`、`reverse_edges`、`rename_type` | 受现有图操作参数约束；若使源记录、已声明关系或查询语义丢失，则拒绝候选。不能为了加速删除验收未覆盖的源数据。 |
| 本项目：重复多跳查询优化 | `materialize_join` | 为连续两／三步 FK 路径建立快捷边，记录实际中间实体与路径多重性；执行器验证快捷路径完整性后使用。 |

`inspect_schema`、`evaluate_queries`、`set_workload`、`finish` 是子代理内部控制工具。转换工具不会直接修改线上数据集；它们先生成候选副本，再进入验收和选择流程。

## 自演化、完整性与候选选择

1. 完整解析当前输入，构建 PK/FK 初始图。
2. 根据领域、schema 和具体样例生成工作负载，校验数量／语义／去重后冻结。
3. 在初始图上执行冻结计划，得到基线正确率、逐条失败信息和耗时。
4. 工作负载冻结且基线测量完成后，按领域、结构和查询特征检索经验，调整当前候选的优先级和 LLM 上下文。LLM 根据反馈选择工具，并给出简短候选理由；无效、重复、不改善或回归候选均返回明确反馈，供下一轮调整。
5. 每个候选先检查当前表示能否完整保留所有输入行、属性、类型／排序语义及已声明 FK，再执行冻结查询。正确率改善优先；正确率都为 100% 时，只有达到最小测量收益阈值的候选才被接受。
6. 保留当前获胜图，丢弃不接受的候选。LLM 模式至少需要一个有效候选进入 oracle 评估；调用无效工具不能充当完成自演化。
7. 结束前重新检查冻结哈希和全输入完整性，再次执行全部查询。总预算已超时或正确数／总数不等于冻结查询数时，构图失败。
8. 最终查询全对后，把本次可归类的候选观测写入构图模块私有经验库，更新后续任务的策略优先级。未通过最终查询或完整性验收的任务不会在该流程中写入正向经验。

经验表示通过独立 oracle 验证的构图实验，不是已发布数据集的目录。若随后报告或图的发布因 I/O 失败，已提交的实验经验仍可能保留；该发布失败不使已验证的实验度量失效。

这里的 self-evolve 包含两个层次：本次图 schema 的“候选—验证—反馈—调整”闭环，以及跨任务的领域策略记忆更新。后者让历史实测影响今后候选的排序，但不训练模型权重、不修改工具代码，也不改变主 agent 的记忆或策略。更准确地说，这是带领域策略记忆的查询驱动构图自优化；不能由“调用过工具”或“检索到经验”推出性能已改善。

### 领域策略、先验和跨任务证据

领域类别固定为金融、学术、电商、医疗、社区、通用六类。`auto` 按业务描述和 schema 名称中的规则信号识别；信号不足或多个领域难以区分时回退到 `general`。分类不从单元格内容推断；报告中的分类置信度是规则分数，不是校准后的概率。业务已明确时可显式选择类别。

各领域有手写策略先验，明确标记 `unvalidated_prior`。这些先验可在没有经验库或关闭经验时使用，并不表示系统已经从数据中学到了成功策略。例如：

- 金融工作负载若频繁沿交易—账户—客户做多跳查询，可优先评估 `materialize_join`；若考虑将交易行转成边，仍须保留每笔交易、金额、币种、时间和账户角色。大量简单余额查询可能不适合相同方案。
- 学术工作负载若频繁查询论文—作者—机构路径，可优先评估路径物化；共享作者或机构实体需要可靠身份键，不能仅因同名而合并。主题展开也必须保留源字段、位置和重复项。

这两个例子是待检验的条件策略，不是已学成功案例。不存在已被证明适用于所有金融或学术数据的唯一最佳图。

可持久化的 `strategy_id` 共 12 种，与上表工具对应：`materialize_join`、`collapse_node`、`extract_property_node`、`node_property_to_edge`、`connect_columns`、`generate_entity_table`、`unfold_multi_category`、`add_primary_key`、`remove_primary_key`、`reverse_edges`、`rename_type`、`delete`。历史记录不包含可直接重放的工具参数；新任务须根据当前数据构造候选。无法安全归类的工具调用或混合策略批次仍可验收，但不贡献可迁移的策略证据。

经验按领域分区，再根据表／行／列／FK 数量、复合键和可空关系、路径长度、查询类别及其比例等结构特征匹配。检索结果只提供排序提示，不批准工具执行，不改变冻结工作负载，也不免除新图的 oracle 和完整性检查。

观测结果分为 `accepted`、`no_gain`、`unsafe`。`mean_gain` 只汇总查询全对的 `accepted`／`no_gain` 实测收益；例如降低 2% 成本但未达到默认 5% 接受门槛，也属于 `no_gain`。它与策略奖励分数不同：`no_gain` 会降低排序，`unsafe` 没有有效速度收益，并带来更强的降权。后续真实观测可以覆盖同源同模式的旧证据。

来源去重使用解析后规范化的逻辑内容哈希，包含 CSV 内联表、schema 和所有行；文件名、SQL 排版和行顺序不作为新的证据来源。同一来源、领域、策略模式只保留最新观测，反复运行不能累加为多份支持。报告中的 `independent_sources` 实际是不同来源指纹的数量，并不证明这些数据在统计意义上相互独立；复制数据后改动内容也可能产生新指纹。

`confidence_status` 的 `candidate`、`supported`、`cautioned` 是规则标签。当前 `supported` 至少要求三个不同来源指纹、接受比例不低于三分之二、没有 `unsafe` 且平均有效收益为正；这个标签仍不是跨数据集普遍有效的证明。库中最多保留最近 2,048 条来源—模式证据和 256 个模式；不是无限增长的完整实验档案。

### 经验存储与隐私边界

默认 Graph Engine 从存储层已校验的保留源路径取得用户作用域，把经验写入：

```text
$IGRAPH_DATA_ROOT/users/{user_id}/construction-experience/strategies.sqlite3
```

同一用户的多个数据集可按领域复用这个库；不同用户不共享，主 agent 不读写它。用户身份和库路径不从 options、业务描述或模型参数取得。CLI 和程序调用使用调用方显式选择的私有作用域。

经验库只存白名单内的结构计数／枚举、策略模式、实测指标及来源／运行哈希，不存原始单元格、表列名称、用户名称、SQL、提示词、完整工具参数或历史查询答案。哈希和结构统计仍属于私有数据；这不是匿名化或加密保证。本次构图的私有报告可能包含 schema、样例和工具参数，不能与受限经验库混为一谈。

库不可用、损坏或权限错误会在报告中标为 `unavailable`；写入失败标为 `write_failed`，并记录受限错误类型或写入状态。经验存储失败不阻止已通过完整验收的合法图交付。关闭经验后不会访问或写入该库。

### 当前图表示不依赖历史图恢复答案

源行由 `table`、`row_index` 和实际存储的 `data` 标识。类型 affinity／collation 等关系 schema 元数据保留在相应的当前行实体上。结构转换后：

- 桥接行数据存储在当前边本身，`row_endpoints` 描述该边的实际端点和 FK 角色。
- 提取属性从当前属性节点的值及当前关联边读取。
- 迁移属性从当前关联边的 `relocated_attributes` 读取。

`representation.resolve_rows` 仅访问传入的当前图，检查映射、冲突和缺失；它不接收源表，不读取旧图快照，也不会用 provenance 中的历史数据弥补损坏结构。执行器可在准备阶段为这些当前实体建立访问索引，这部分成本计入 `preparation_ms`。

全输入完整性检查会使用源数据作保真对照，但不负责提供图查询答案。SQL oracle 与图执行是分开的：SQL 输入的 oracle 克隆原内存 SQLite 数据库，保留真实 SQL schema；CSV oracle 按原 JSON 标量建立独立表；图执行器只访问当前图。

## 结果、报告与性能含义

正常数据集构图的私有报告位于：

```text
$IGRAPH_DATA_ROOT/users/{user_id}/datasets/{dataset_id}/construction/evolution-report.json
```

它与该数据集的保留源、`record.json` 同目录；不会被塞进主 agent 会话状态。CLI 报告则位于本次 `--output` 目录。报告包含配置、源哈希、源警告、冻结查询及哈希、基线与最终结果、候选历史、接受原因、拒绝原因、图规模、阶段和失败代码等。

跨任务部分可查看 `domain_profile`，以及 `experience` 中的 `enabled`、`status`、`domain_key`、`context`、`retrieved`、`candidate_priorities`、`write`、`recorded_observations`、`policy_after`；字段是否出现取决于实际执行阶段。`retrieved` 是本次开始优化时的历史摘要，`candidate_priorities` 是当前候选的排序依据，`policy_after` 是成功写入后重新检索的策略状态。`recorded_observations` 表示本次写入的观测条数，不能当作新增独立来源数。`source_sha256` 是原文件哈希，不等于用于经验去重的规范逻辑来源指纹。

成本目标为：

```text
cost_ms = latency_ms + preparation_ms / expected_query_runs
```

`latency_ms` 是各查询多次执行耗时中位数之和；`preparation_ms` 是当前图表示和索引准备时间；`oracle_latency_ms` 单独记录，不作为图速度。这个目标估计同一图被重复查询时的摊销成本，不是完整构图端到端耗时。

所有耗时来自本模块进程内真实属性图执行器，包括真正使用的快捷路径。它不是 NebulaGraph 或 Galaxybase 的生产部署基准。小样本测量受计时噪声影响；是否值得部署应结合报告中的实际接受候选、最终成本、工作负载及目标运行环境判断。

“完整性检查”覆盖本次导入的全部输入数据；若输入本身是公开数据集样本，检查范围仍然只是该样本。“查询 100%”表示完整结果按重复行、NULL、排序／确定性 limit 和数值比较规则与独立 SQL 对照一致；报告中的预览行数不是比较范围。

## 服务器验收与公开样本

验收在独立工作目录中完成。公开示例使用以下占位路径，复现时替换为自己的目录：

```text
/path/to/igraph-acceptance
```

原始公开 4DBInfer StackExchange 数据目录以 `/path/to/galaxyweaver/data/stackexchange` 表示。样本准备工具见 [prepare_construction_benchmark 说明](../scripts/prepare_construction_benchmark.md)，读取 `metadata.yaml` 和 parquet，按有限初始行递归补齐引用，并输出 SQL 与独立 manifest。原始验收数据、SQL 导出、完整报告、经验数据库及归档文件不随源码发布，由维护者在本地留存。

### 2026-09-09：领域策略记忆版本回归

服务器最终 Python 回归 **239 项通过，另有 88 个子测试通过**，用时 21.57 秒；11 条警告来自依赖弃用提示。前端在 Node 22 下通过 TypeScript 检查和 Vite 构建，31 项测试通过。对应 Python 用例清单和前端结果由维护者作为本地验收记录留存，不随源码发布。

随后在四份明确标注为合成、非生产的数据上真实调用 `qwen3.6-plus`。同领域两份数据具有相同 schema、不同主键和业务取值，全部外键闭合；运行使用独立模型进程及同一个构图专属经验库，每个领域先进行首轮再进行后续任务。

| 领域与运行 | 输入行 | 检索历史策略 | 写入观测 | 最终正确数 | 接受结构候选 |
|---|---:|---:|---:|---:|---:|
| 金融首轮 | 75 | 0 | 3 | 15/15 | 0 |
| 学术首轮 | 79 | 0 | 3 | 15/15 | 0 |
| 金融后续（重试成功） | 208 | 3 | 2 | 15/15 | 0 |
| 学术后续 | 205 | 3 | 1 | 15/15 | 0 |

金融后续的首次尝试在查询生成校验阶段失败，未发布图、未写入经验。保留该失败报告后，补充了 `workload_validation_errors` 诊断，并明确允许模型复用已验证的 seed 计划、改写具体业务问题；未放宽 DSL 或正确性条件。随后重试成功。

本轮验证了领域隔离、跨任务持久检索、当前 schema 参数绑定和策略优先级更新。例如，金融后续的路径物化优先级由纯先验 `0.4` 降至 `0.372`，学术后续降至 `0.3688`，反映历史候选未达到收益门槛。所有成功运行均由 LLM 生成冻结查询；最终图下载后再次核对源／查询／图哈希、全输入完整性并执行 SQL 对照，仍全部正确。

本轮真实记录以 `no_gain` 等未接受证据为主，四次均未接受结构候选；不能宣称已经找到金融或学术领域的最优 schema，也不能把模型调用耗时差异解释成学习后的加速。正向证据提升优先级、失败证据降权和新数据回归拒绝另有使用真实查询 oracle 与受控计时的单元测试，这些受控计时不是性能实验。

跨任务验收摘要、下载后复核结果和合成输入 manifest 均由维护者在本地留存，不随源码发布。manifest 记录了来源、行数、哈希及预检范围。

### 2026-09-08：加入跨任务经验前的真实 LLM 验收

以下为旧版本的流程和查询正确性证据，不是新增领域策略记忆的验收结果。

两次真实 LLM 验收均使用 `qwen3.6-plus`、`mode=llm`，报告中 `llm_used=true`、`workload_origin=llm`。每次生成并冻结 15 个查询，图查询每条重复测量 5 次，准备成本按 100 次查询摊销，候选速度收益阈值为 5%。两个样本均包含 9 张真实表，无虚拟表；它们是具有来源记录的 FK 闭合样本，不代表原始全量数据。

| 运行 | 输入行／初始节点 | 初始 FK 边 | 最终正确数 | 优化轮次／工具调用 | 进入评估的有效候选 | 接受候选 | 总耗时 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `live-02` | 67 | 79 | 15/15 | 6 / 8 | 3 | 0 | 75.671 秒 |
| `live-larger` | 1,591 | 2,514 | 15/15 | 5 / 7 | 2 | 0 | 70.546 秒 |

`live-02` 评估了两个 `materialize_join` 候选和一个 `optimize_graph` 候选；这些候选均保持 15/15，但测得的成本降低不足 5% 阈值，因此全部拒绝。`live-larger` 评估了两个 `materialize_join` 候选，其对照成本分别从约 `7.753315 ms` 增至 `7.798088 ms`、从约 `7.730379 ms` 增至 `7.906157 ms`，也全部拒绝。

两次最终都保留了通过验收的初始 PK/FK 图，节点数和边数未变。结果验证了真实模型生成工作负载、调用工具、接收候选反馈、拒绝未达到收益要求的变更及最终完整验收的流程；本轮没有取得经接受规则确认的结构加速。报告中同一基线图多次计时出现的小幅差异不能解释为图结构优化收益。

`live-02` 和 `live-larger` 完整报告由维护者在本地留存，不随源码发布。报告包含冻结查询、逐条结果、候选成本、接受状态、源 schema 和哈希。样本 manifest 另外区分原始总行数、实际样本索引、闭包补齐行及缺失引用造成的整行排除，不能将样本中的计数直接解释为全站或全数据集统计。

该旧版本服务器 Python 回归为 **197 项通过，另有 57 个子测试通过**，包含完整 BFF → 原生 Runtime／MCP → Engine → BFF 的用户与项目作用域检查，以及真实进程中断后的恢复；11 条警告来自现有依赖的弃用提示。Windows 本地构图存储的 23 项测试也全部通过，覆盖实际跨进程锁行为。前端在 Node 22 环境下通过 TypeScript 检查和 Vite 构建，相关 3 个测试文件中的 29 项测试通过。旧版本测试清单和两份图的下载后复核结果由维护者在本地留存，不随源码发布。这些旧版本回归和上述两次真实 LLM 测试各有明确范围，不代表已完成 NebulaGraph 生产规模性能测试。

### 在自己的环境运行检查

以下命令运行源码中随附的部分相关测试，不需要上述私有验收产物，也不代表重放全部历史服务器验收。先在 Python 3.11 或更新版本的环境中安装项目依赖及 `pytest`，再从项目根目录运行：

```sh
python -m pytest \
  tests/servers/test_construction_domain.py \
  tests/servers/test_construction_experience.py \
  tests/servers/test_construction_domain_learning.py \
  tests/servers/test_tabular_construction_agent.py \
  tests/test_construction_bff_timeout.py
```

前端使用 Node 22.12 或更新版本及项目指定的 pnpm，安装依赖后执行构图相关测试与构建：

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend test src/components/GraphConstructionPanel.test.tsx src/components/ConstructionEntrypoints.test.tsx src/construction.api.test.ts
pnpm --dir frontend build
```

真实 LLM 复现还需按前文配置模型，并提供自己的 SQLite／CSV 输入；工作负载和耗时会随输入、模型响应及运行环境变化。

相关代码与测试：

- [构图流程和参数](../graph_engine/construction/agent.py)
- [领域识别、策略先验与候选排序](../graph_engine/construction/domain.py)
- [受限经验存储与证据汇总](../graph_engine/construction/experience.py)
- [跨任务经验接入与隔离测试](../tests/servers/test_construction_domain_learning.py)
- [经验去重、负反馈与存储测试](../tests/servers/test_construction_experience.py)
- [输入解析与全数据完整性](../graph_engine/construction/sources.py)
- [工具适配](../graph_engine/construction/tools.py)
- [当前实体表示](../graph_engine/construction/representation.py)
- [查询生成、SQL oracle 与图执行](../graph_engine/construction/workload.py)
- [构图专用 HTTP 超时测试](../tests/test_construction_bff_timeout.py)
- [公开样本准备测试](../tests/test_prepare_construction_benchmark.py)
