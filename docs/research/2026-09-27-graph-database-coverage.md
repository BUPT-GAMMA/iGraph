# 远程图数据库覆盖调研

日期：2026-09-27。状态：设计输入，非实现或兼容性验收报告。只阅读官方资料，未部署本表中的新数据库，也未验证写入拦截效果。

## 结论

目标应是分批覆盖常见属性图和 RDF 图数据库，而不是只支持一种 Cypher 数据库。下面的候选不是市场份额排名，也不是声称已穷尽所有主流产品。

建议保持现有 Agent 和结构发现、查询两个工具；各产品独立 Adapter，按真实协议、查询语言和能力描述连接。不做跨语言查询翻译，不把 Bolt/Gremlin 兼容等同于所有安全及元数据能力兼容。

建议第一批技术验证覆盖 **FalkorDB、Memgraph、NebulaGraph**，其中 FalkorDB 可优先：官方提供由服务端拒绝写查询的 `GRAPH.RO_QUERY`，区别于仅影响路由的 READ 模式。Memgraph 与 NebulaGraph 的查询分类、防副作用机制仍需专项验证，不能预先承诺第一批全部达到完整支持。[FalkorDB 只读命令](https://docs.falkordb.com/commands/graph.ro-query)、[Neo4j 路由不是访问控制](https://neo4j.com/docs/python-manual/current/transactions/)

## 覆盖与接入差异

| 产品 | 查询与接入 | 结构、参数与验证重点 |
| --- | --- | --- |
| Neo4j | Cypher / Bolt，官方 Python driver | 保留现有已验收实现；driver READ_ACCESS 无安全保证，不能单独作为写入拦截。[官方 API](https://neo4j.com/docs/api/python-driver/current/api.html) |
| Memgraph | Cypher / Bolt，可用 Neo4j Python driver 或 GQLAlchemy | 官方示例支持参数；共享 driver 不代表共享 schema procedure、EXPLAIN 结果及函数清单。需验证指定版本的元数据发现、写查询分类及扩展过程副作用。[Python 接入](https://memgraph.com/docs/client-libraries/python) |
| FalkorDB | openCypher / Redis 协议，FalkorDB Python SDK | `ro_query` 有参数、超时；使用 graph 名称而非 Neo4j database。节点/边返回类型与 schema discovery 需独立实现。只向模型开放查询工具，不开放 Redis 任意命令。[SDK](https://falkordb-py.readthedocs.io/en/stable/falkordb.html)、[只读命令](https://docs.falkordb.com/commands/graph.ro-query) |
| NebulaGraph | nGQL，Nebula Python 客户端 | 有 graph space 和 session/connection pool；官方示例以 `SHOW TAGS` 获取类型，参数执行 API 见客户端仓库。不可按完整 openCypher 对待；只读语法分类、复合语句、函数及管理命令需专门验证。[3.8 客户端](https://docs.nebula-graph.io/3.8.0/14.client/5.nebula-python-client/)、[官方客户端源码](https://github.com/vesoft-inc/nebula-python) |
| Apache HugeGraph | Gremlin / HTTP REST | 有独立 schema API；Gremlin 请求提供 bindings/aliases。官方提醒原生查询端点有安全风险，默认示例为 gremlin-groovy，不能直接把模型生成脚本透传。[Gremlin API](https://hugegraph.apache.org/docs/clients/restful-api/gremlin/)、[REST API](https://hugegraph.apache.org/docs/clients/restful-api/) |
| JanusGraph | Gremlin Server，Gremlin-Python | 官方 Python 示例用 WebSocket 与遍历构造；特有类型可能需要 JanusGraph serializer。schema 管理及通用 Gremlin 采样不是同一能力，需区分完整结构与采样。[Python 文档](https://docs.janusgraph.org/master/basics/connecting/python/) |
| Amazon Neptune | openCypher、Gremlin、SPARQL；可先做 openCypher Data API/Boto3 | 三种语言不能按一个结果模型无条件复用。AWS IAM 提供查询动作权限；凭据签名、网络可达性、引擎版本和远程测试单独验证。[语言与权限限制](https://docs.aws.amazon.com/neptune/latest/userguide/limits.html)、[IAM 数据动作](https://docs.aws.amazon.com/neptune/latest/userguide/iam-dp-actions.html)、[Python 查询示例](https://docs.aws.amazon.com/neptune/latest/userguide/opencypher-query-hints-timeout-hint.html) |
| Azure Cosmos DB for Apache Gremlin | Gremlin API，专用云连接配置 | 官方明确不支持 Gremlin Bytecode，需要文本遍历；因此不能直接复用 JanusGraph 的全部执行策略。数据库、容器、分区以及 serializer/driver 版本需单独验收。[兼容性](https://learn.microsoft.com/en-us/azure/cosmos-db/gremlin/support)、[官方 Python 示例](https://github.com/Azure-Samples/azure-cosmos-db-graph-python-getting-started) |
| ArangoDB | AQL / HTTP 查询与 cursor API | API 提供参数、解释/验证、跟踪和终止查询。需要专用 AQL 修改语句及函数策略；EXPLAIN 成功不等于只读。图定义、文档集合、边集合不能机械套用 Neo4j 标签。[AQL HTTP API](https://docs.arango.ai/arangodb/stable/develop/http-api/queries/aql-queries/) |
| TigerGraph | GSQL，REST API / pyTigerGraph | SDK 区分 schema、安装查询、执行查询与通用 GSQL 等能力。只开放已安装查询不等于满足“Agent 自己编写查询”；解释执行的版本、参数及只读约束需专项验证，禁止偷偷安装查询。[pyTigerGraph](https://github.com/tigergraph/pyTigerGraph)、[SDK 能力](https://www.tigergraph.com/docs/pytigergraph/1.7/core-functions/)、[解释查询示例](https://docs.tigergraph.com/tigergraph-server/4.1/data-loading/read-to-spark-dataframe) |

## 写入拦截、超时与取消

以下为设计判断，不是已经验证的安全保证：

- 账号是否只读不恢复为接入门槛；但每个 Adapter 的应用执行路径仍必须有经验证的写入/DDL/管理操作阻断策略。无法分类的语法、动态执行及扩展过程应拒绝，不可退回关键词黑名单后宣称安全。
- 只读还涉及外部访问与副作用。Gremlin-Groovy 是脚本入口，不是只检查 `addV/drop` 就足够。考虑严格语法/遍历子集及服务端保护，不能把模型查询交给 Python `eval`、Groovy 任意求值或允许其移除保护策略。
- FalkorDB 的只读命令是较直接的服务端能力，但仍要验证目标版本、过程调用、资源限制和可写账号下的负面用例，不能由一页文档推导无条件安全。
- 客户端等待超时不代表服务器已停止。Adapter 应分别表达服务器期限、客户端期限及取消能力；取消权限不足应明确反馈，不为取消而向 Agent 开放管理工具。
- Memgraph 官方发布记录包含 query timeout 和 TERMINATE TRANSACTIONS，且过程配合取消存在版本差异；应固定版本实测。[发布记录](https://memgraph.com/docs/release-notes)
- Neptune 有每查询超时，和集群期限取较小值，超过后由服务端终止。[官方超时说明](https://docs.aws.amazon.com/neptune/latest/userguide/opencypher-query-hints-timeout-hint.html)
- TinkerPop 不同大版本配置/传输存在差异，不能拿 master 文档替代产品锁定版本的配置；3.x 有 evaluationTimeout 文档。[3.4 API](https://tinkerpop.apache.org/javadocs/3.4.4/full/org/apache/tinkerpop/gremlin/server/Settings.html)、[当前源码配置](https://github.com/apache/tinkerpop/blob/master/gremlin-server/conf/gremlin-server.yaml)
- ArangoDB 查询 API 有 maxRuntime 和查询终止能力；TigerGraph REST 有查询超时配置，但解释/安装路径要分别测。[ArangoDB](https://docs.arango.ai/arangodb/stable/develop/http-api/queries/aql-queries/)、[TigerGraph REST](https://www.tigergraph.com/docs/tigergraph-server/4.1/api/)

NebulaGraph 的目标版本取消、Memgraph 的查询类型判别、Gremlin 严格语法路径、TigerGraph 任意只读解释查询、各产品的自定义过程/函数均属本次未解决项。不能从“没查到可靠机制”推断产品没有该能力。

## 本地测试与许可边界

本地部署可行性与生产授权是两件事，连接已有服务器也不等于可以重新分发其镜像或提供托管服务。以下仅为工程可行性线索，不构成许可法律判断：

- Memgraph 有 Docker 快速开始；FalkorDB、NebulaGraph、JanusGraph、HugeGraph 的官方仓库提供源码、许可及部署入口，可优先建立隔离的本地测试环境。执行前锁定版本并读该版本许可，不能把社区版一律称为 Apache 开源。[Memgraph](https://memgraph.com/docs/client-libraries/python)、[FalkorDB](https://github.com/FalkorDB/FalkorDB)、[NebulaGraph](https://github.com/vesoft-inc/nebula)、[JanusGraph](https://github.com/JanusGraph/janusgraph)、[HugeGraph](https://github.com/apache/hugegraph)
- ArangoDB 3.12 存在许可模型变化，不能沿用旧版许可结论。[官方不兼容变更](https://docs.arango.ai/arangodb/stable/release-notes/version-3.12/incompatible-changes-in-3-12/)
- TigerGraph 当前版本对比列出可在 Linux/Docker 自托管的 Community Edition；具体镜像、开发许可领取和限制需以选定版本核实，不将旧文档中“必须商业许可”当作统一阻塞。[当前版本对比](https://www.tigergraph.com/docs/tigergraph-server/4.2/intro/comparison-of-editions)
- Neptune / Cosmos 的正式兼容性验收需真实云实例、账号、网络和预算。不能仅凭本地模拟器或兼容服务器宣称已通过，也不能让缺少云资源阻塞所有本地 Adapter。[Neptune](https://docs.aws.amazon.com/neptune/latest/userguide/limits.html)、[Cosmos](https://learn.microsoft.com/en-us/azure/cosmos-db/gremlin/)

## RDF / SPARQL 后续路线

GraphDB、Stardog、Virtuoso 应列入后续覆盖，而非把“图数据库”限定为属性图。三者均有 SPARQL/HTTP 接入，但 repository/database、认证、推理开关、命名图和结果类型仍有差异。[GraphDB 接口](https://graphdb.ontotext.com/documentation/10.1/getting-started.html)、[Stardog HTTP](https://docs.stardog.com/developing/http-api)、[Virtuoso SPARQL](https://vos.openlinksw.com/owiki/wiki/VOS/VOSSparqlProtocol)

此路线需单独验证许可、部署、参数表达、超时和取消。不能仅允许 SELECT 就宣称无副作用：Virtuoso 官方描述了查询期间获取外部 RDF 并存储的扩展机制。应审查 SERVICE、扩展函数和厂商 pragma，并限制外部网络访问。[Virtuoso Sponger](https://docs.openlinksw.com/virtuoso/virtuosospongerusage/)

## 建议分批与支持等级

1. 第一批：现有 Neo4j 回归；FalkorDB、Memgraph、NebulaGraph 做安全技术验证并逐个交付。可优先 FalkorDB，不承诺三者同时完整支持。
2. 第二批：ArangoDB、JanusGraph、HugeGraph；复用通用连接和结果契约，独立实现方言、schema、安全校验与取消。
3. 第三批：Neptune、Cosmos 云接入以及 TigerGraph；依据账号环境与真实业务需求调整顺序，不让云资源拖住本地交付。
4. RDF 路线：GraphDB、Stardog、Virtuoso，先验证一个，再扩大兼容面；如用户已有 RDF 数据可提前。

建议 UI/文档区分“规划中”“实验接入”“已验证支持”。只有完成指定版本的真实连接、结构发现、参数查询、节点/边/路径序列化、写入拦截负例、超时/取消、跨连接隔离及真实 Agent loop 才标记已验证。实验状态不允许绕过写入安全，安全机制未证实时只能在专用测试环境探索。

每种数据库使用独立导入脚本和同一逻辑测试数据，明确有向边/去重规则，不能只对齐节点关系数量。新增 Adapter 不等于已经解决现有模型回答证据质量问题，二者分别验收。
