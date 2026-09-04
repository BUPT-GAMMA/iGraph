# iGraph Agent 系统功能实测与修复报告（2026-09-04）

## 1. 结论

本轮已在 `igraph-front` 服务器 Compose 部署上完成真实浏览器、真实数据集、真实 Agent 工具调用和容器内自动化测试，并将修复后的代码部署到服务器。最终六个服务均为 `healthy`。

核心可用链路：用户进入 → 创建项目 → 上传/切换项目数据集 → 普通与多轮对话 → 图信息/中心性/最短路 → GCN 节点分类、链路预测、HIV 图分类 → 指定对象推理 → 暂停/继续接收 → 页面与会话恢复。

本轮没有把“已存在代码路径”当作“实测通过”。未覆盖或当前未实现的条目继续保留在检查单中。

## 2. 测试环境与数据

- 部署目录：`/home/tanjiarui/iGraph-front`，Compose project 为 `igraph-front`。
- 本地访问：经 SSH 端口转发访问已部署前端；未直接暴露内部 Backend、Agent Runtime、Graph API、Graph MCP 和 PostgreSQL。
- 测试用户：`admin`。
- 隔离测试项目：`系统验收-20260904-A`，项目 ID `90a70790-2dbd-4e11-91f7-c8d8fb178cb0`。
- 测试会话：`c4a93b07-ec7f-403e-97cf-aae244d35d44`。
- 数据文件：`bail.pt` 7,976,262 B、`cora.pt` 20,767,310 B、`hiv.pt` 108,125,602 B、`dgraphfin.pt` 680,319,145 B。
- `hiv.pt` 是多图图分类数据集；其余文件按单图节点分类/链路预测数据使用。
- `hiv.pt` 因体积较大，通过项目数据集 API 上传；浏览器文件上传链路已用 `bail.pt` 和 `cora.pt` 实测。`dgraphfin.pt` 超过当前 Nginx 512 MiB 限制且 Backend 会整文件读入内存，本轮未冒险上传。

## 3. 可用功能与实测证据

| 功能 | 结果 | 证据摘要 |
| --- | --- | --- |
| 用户入口与错误提示 | 通过 | `admin` 可进入；空 ID 显示“请输入用户 ID”；不存在用户被明确拒绝。真实身份认证仍未实现，见残余风险。 |
| 项目与基础界面 | 通过 | 创建隔离项目成功；General 可见；侧边栏、最近任务、深色主题及刷新保留正常。 |
| 会话与普通对话 | 通过 | 首次非空发送才创建会话；空消息拦截；Shift+Enter 换行；`2+2` 与后续指代问题可连续回答。 |
| 数据集上传与切换 | 通过 | 浏览器上传 `bail.pt`、`cora.pt`；项目 API 上传 `hiv.pt`；默认项唯一；会话可在数据集间切换并在刷新后保持。 |
| 单图结构分析 | 通过 | Cora：2,708 节点、5,278 条无向唯一边；中心性 Top 节点为 163、565、747；163 到 565 的最短路为 163→523→565（2 hops）。 |
| 图工作区 | 通过 | Overview/Data/Graph 页签可用；单图可视化正常；HIV 41,127 图集合明确提示不能当作独立单图直接可视化。 |
| 节点分类训练与推理 | 通过（GCN） | 单次 Job `b6b7d00bd6f7`，模型 `mdl_node_classification_1788492315_87cffc`；显式 `epochs=3`、`hidden_dim=16` 均保留，实际训练 3 轮；val_acc 54.43%。节点 0/1/2 返回预测类别与完整概率。 |
| 链路预测训练与推理 | 通过（GCN） | 单次 Job `e90bb1ef2590`，模型 `mdl_link_prediction_1788495886_28127d`；实际训练 2 轮，val_acc 70.83%，val_auc 0.7649。边 `(0,8)`、`(0,1)`、`(1,344)` 均返回分数、预测和概率。最终契约回归只调用 1 次 `infer`。 |
| HIV 图分类训练与推理 | 通过（GCN） | 数据集 `gds_680ccc63`；Job `b23f5beef7ee`，模型 `mdl_graph_classification_1788495795_e899a8`；实际训练 2 轮，val ROC-AUC 0.6107、test ROC-AUC 0.6353。graph indices 0/1/2 均返回概率、预测和真实标签。 |
| 异步训练轮询 | 通过 | 三类训练都只提交一次 `train`，随后轮询同一 `job_id` 到终态；未通过删除用户配置或重复训练来规避错误。 |
| 暂停/继续接收 | 修复后通过 | 运行中显示“暂停接收（Agent 将继续运行）”；暂停后本页断开 SSE、后台 Run 继续。暂停 25 秒待后台完成，再继续接收可恢复完整 15 段输出且不创建第二个 Run。 |
| 容器与网页健康 | 通过 | frontend、backend、agent-runtime、graph-engine-api、graph-engine-mcp、postgres 最终全部 `healthy`；浏览器在 Agent/MCP 滚动更新后可继续使用原会话。 |

说明：GAT、GraphSAGE、Node2Vec、PCBA、非法节点/图版本、GPU/OOM 等扩展矩阵未在本轮逐一运行，因此 G01–G04 等综合条目仍不能整体勾选。

## 4. 本轮已修复功能

1. **MCP 嵌套对象兼容**：`train_config`、`algo_config`、`data`、`query`、`infer_config` 现在同时接受对象和 Agno 发送的 JSON 字符串，并在转发 Graph API 前统一解码。修复了显式训练参数被 FastMCP 提前拒绝的问题。
2. **训练单 Job 与参数保真**：训练技能更新为只提交一次异步训练、轮询同一 `job_id`、保留用户显式配置，不再通过删除配置或重复提交来“重试成功”。
3. **链路推理参数契约**：工具描述和推理技能明确 `edges` 必须是 `[[src,dst], ...]`，并给出节点、源节点和图索引示例。部署后的最终回归首次即成功，只调用 1 次 `infer`。
4. **Graph MCP 身份头健壮性**：header provider 在没有 `RunContext` 时返回空字典，消除 `NoneType.user_id` 异常路径；有上下文时仍传递用户、会话、Run、Agent 标识。
5. **暂停观看与终态恢复**：新增暂停/继续接收控件、每用户/会话 SessionStorage 状态、权威 Run ID 接管，以及“恢复流已经结束但本地仍认为活动”的终态对账，避免无限重连。
6. **项目输入校验与 422 可读性**：创建/编辑项目在提交前校验 80 字符名称与 500 字符描述；API 客户端把 FastAPI `detail[]` 格式化为字段路径和消息，而不是泛化的 `422`。
7. **回归测试**：补充 MCP object/string schema、Graph header provider、暂停/继续接收、Run ID 接管、422 格式化、GraphML 推理示例等回归测试。

## 5. 自动化测试与构建

| 检查 | 结果 |
| --- | --- |
| 前端 Vitest | 8 个测试文件、60 项测试通过 |
| Graph MCP unittest | 11 项通过 |
| Agent Runtime policy/header unittest | 3 项通过 |
| GraphML 契约 unittest | 10 项通过 |
| 前端生产 TypeScript/Vite build | 通过 |
| Graph MCP / Agent Runtime / Frontend 镜像构建 | 通过 |
| 最终 Compose 健康检查 | 6/6 healthy |

上述自动化共执行 84 项测试，0 项失败。测试在对应 Docker 运行时中执行，避免本机 Python 版本与项目 Python 3.12 要求不一致。

## 6. 仍未解决或未完整覆盖

### 必须继续处理的产品/架构缺口

- **S01 真实认证**：当前主要信任 `X-User-ID`，不能视为公网可信登录。
- **S02 CORS/网络安全基线**：内部服务未映射到宿主机是正确的；但应用级 CORS 与可信代理/认证策略仍需收紧。
- **S03 自定义模型密钥**：API Key 仍以明文 JSON 保存，需要加密或外部 Secret/KMS。
- **BF37 大文件保护**：代理有 512 MiB 上限，但 Backend 仍整文件读取，缺少一致的业务大小、并发、超时和内存保护；`dgraphfin.pt` 本轮因此未上传。
- **H03 训练取消**：取消外层 `asyncio` Task 不保证立即停止 `to_thread` 中的 CPU/GPU 训练，需要协作式 cancellation token。
- **H04–H09 训练恢复**：Job ledger 仍在内存中，没有 checkpoint 续训、幂等键、租约/心跳、断点兼容校验和恢复可观测性。
- **暂停语义边界**：本轮实现的是“暂停观看/继续接收”；“停止生成”和服务端“暂停执行”尚未实现。
- **Agno 会话上下文告警**：开启 session-context planning 时，上游提取工具偶尔把 `plan` 生成为字符串而非字符串列表，记录 Pydantic validation error；主回答和训练不受影响，但该轮的自动计划上下文不会保存。需升级/修补 Agno 契约或增加兼容层。

### 本轮未执行的完整矩阵

- 停用用户、双用户资源越权、自定义模型真实上游、技能提升/审批、Memory CRUD/SFT 导出。
- 项目/会话删除与移动、General 全部保护路径、附件失败/伪扩展/空文件。
- HITL 接受/拒绝/重复审批、真实网络抖动、多副本路由、Agent Runtime 各阶段故障注入。
- NebulaGraph、文档 RAG、全部图算法、GAT/GraphSAGE/预训练、GPU/OOM、Graph Engine/宿主机强制中断。

这些项目继续保持未勾选或 🟡/❌，不能由本轮核心冒烟结果推断为通过。

## 7. 变更文件

- `mcp_service/tool_forwarding.py`
- `mcp_service/server.py`
- `agent_runtime/graph_tools.py`
- `agent_runtime/resources/skills/gnn_node_classification_training/SKILL.md`
- `agent_runtime/resources/skills/graph_ml_training/SKILL.md`
- `agent_runtime/resources/skills/graph_ml_inference/SKILL.md`
- `graph_data_service/tool_catalog.py`
- `frontend/src/api.ts`
- `frontend/src/components/ProjectDialogs.tsx`
- `frontend/src/components/Composer.tsx`
- `frontend/src/main.tsx`
- `frontend/src/sessionState.ts`
- 对应前端与 Python 测试文件

服务器最终运行的是以上更新后的源代码所构建镜像；测试项目和会话被保留，便于复验。
