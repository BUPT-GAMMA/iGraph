# 数据集构图与图数据优化

本功能把 SQL、CSV、TXT 文件的构图流程接入前端、BFF、Graph Engine 和 Agent Runtime。构图结果归属于原 dataset，可保存、重新加载并继续优化；会话只是加载该数据集的某个版本进行分析。

**目前 SQL、CSV、TXT 三种构图算法均未实现，也未注册生产算法。** 当前交付的是数据源保存、算法扩展接口、图结果持久化、可视化、六类优化，以及会话和 Agent 调用链路。未接入算法时，页面显示“构图算法待接入”；API 明确返回 `algorithm_unavailable`，不会用旧 CSV 边表解析、TXT 文本抽取或演示数据代替构图结果。

## 1. 数据源与已有数据的兼容

新上传的 SQL/CSV/TXT 文件先保存到个人或项目数据集，BFF 的 dataset 记录为：

```json
{
  "status": "source_ready",
  "metadata": {
    "dataset_kind": "construction_source",
    "construction": {
      "status": "not_built",
      "revision": 0,
      "source_type": "csv"
    }
  }
}
```

`source_ready` 表示原文件已保存，不表示图已经可用。此类数据集不会自动替换当前会话的图，也不能设置为项目默认图。上传入口和附件入口都保留 dataset 身份，后续构图不需要再次上传文件。

已有的、已经按旧流程加载成功的 CSV/TXT 数据集保持原有加载行为。仅打开这些数据集的构图状态面板，不会把旧 `ready` 数据集改成 `source_ready`。Graph Engine 底层旧上传解析器继续保留兼容能力；新数据源入口由 BFF 状态、MCP 检查和 Runtime 指令导向构图流程。

用户直接粘贴的独立内联边表或文本仍按现有功能处理。Agent 不得读取新源文件后把内容复制到旧文本、三元组或上传工具中绕开构图入口。

| Graph Engine 构图状态 | BFF dataset 状态 | 含义 |
| --- | --- | --- |
| `not_built` | 新文件为 `source_ready` | 尚无保存的构图结果，`revision=0` |
| `algorithm_unavailable` | `source_ready` | 源文件已保留，对应算法尚未注册 |
| `failed` | `source_ready` | 算法执行或输出校验失败；原文件保留，失败信息放在构图状态中 |
| `ready` | `ready` | 已保存可复用图，`revision>=1`，`dataset_kind=single_graph` |

Graph Engine 是构图状态和图数据的权威来源；BFF 的 `metadata.construction` 是数据集目录中的同步信息。查询或使用数据集时会核对 Engine 状态，防止目录信息落后于实际版本。

## 2. 前端入口与编辑流程

“构图”入口放在数据集列表和项目数据集条目旁，构图成功后改为“查看构图”。右侧图工作台也提供入口：当前数据集支持构图时直接打开它，否则进入数据集选择。源文件上传完成后可直接进入构图工作台。

工作台把源文件信息、六项优化表单、图画布、节点/边属性检查和保存动作放在同一处。编辑流程为：

1. 读取构图状态；存在保存图时读取图和当前 `revision`。
2. 从实际节点、边和属性中选择操作目标。
3. “应用并预览”提交完整的待保存操作列表，只更新预览图。
4. 可撤销预览的最后一步或还原全部；关闭含未保存修改的面板时提示用户。
5. “保存到数据集”提交与预览相同的版本及操作列表。
6. 保存成功后，可选择“在当前会话使用”，显式加载保存版本。

画布最多展示 180 个节点和 400 条边。工作台统计、优化校验与保存仍针对完整图数据，不能把画布显示数量当作数据集的完整规模。无算法时不显示伪造的图结果。

## 3. 图文档契约

`GET /construction/{dataset_id}/graph` 返回：

```json
{
  "dataset_id": "ds_people",
  "revision": 1,
  "graph": {
    "directed": true,
    "nodes": [
      {"id": "person_alice", "type": "Person", "properties": {"phone": "13800000000"}},
      {"id": "person_bob", "type": "Person", "properties": {"name": "Bob"}}
    ],
    "edges": [
      {
        "id": "knows_1",
        "source": "person_alice",
        "target": "person_bob",
        "type": "KNOWS",
        "properties": {"since": 2020}
      }
    ]
  }
}
```

内层 `graph` 只允许 `directed`、`nodes`、`edges` 三个字段。节点只允许 `id`、`type`、`properties`；边另外包含 `source`、`target`。名称、标签等业务信息应放在 `properties`，不要添加未约定的顶层字段。

- `directed` 必须为布尔值。
- 节点和边的 `id`、`type` 必须是非空字符串；ID 在各自集合中唯一，不接受自动转成字符串的数字 ID。
- 边两端必须引用存在的节点 ID。
- `properties` 必须是 JSON 对象；支持嵌套 JSON，但拒绝非字符串属性名、`NaN`、无穷数以及 Python 专有对象。
- 节点 ID、边 ID 由算法生成，并应在同一数据源的相同建模结果中保持稳定。优化生成的新 ID 使用确定性哈希，使同一版本、同一操作的预览与提交一致。
- 当前单个构图文档上限为 200,000 个节点和 1,000,000 条边；每次优化批次包含 1 至 100 项操作。

能力接口的 `schema` 值为 `construction_graph.v1`。不要把此字段加入内层图对象；这也不是旧 `graph_data.v1` 数组存储格式。

## 4. BFF 对外 API

个人数据集前缀为 `/api/personal-datasets/{dataset_id}/construction`，并保留 `/api/datasets/{dataset_id}/construction` 别名。项目数据集前缀为 `/api/projects/{project_id}/datasets/{dataset_id}/construction`。

| 方法 | 前缀后的路径 | 请求与用途 |
| --- | --- | --- |
| `GET` | 空 | 查询状态、版本、规模和算法可用性 |
| `POST` | `/build` | `{"options": {}}`，从已登记的数据集源文件发起构图 |
| `GET` | `/graph` | 读取保存的图与版本 |
| `POST` | `/preview` | `{"base_revision": 1, "operations": [...]}`，只预览 |
| `POST` | `/commit` | 与预览相同的请求体，保存到 dataset |

BFF 根据登录用户、dataset 和项目归属完成授权，再从服务器数据集记录读取 `storage_path` 和 `original_filename`。浏览器不提供可信源路径，也不能通过请求体指定所属用户或另一个项目。普通数据集选择流程会使用下面的 Engine 专用加载接口，前端不直接调用内部服务。

构图成功和优化提交后，BFF 同步 `metadata.construction`、节点数、边数、方向和 `dataset_kind`。删除对应个人/项目数据集或项目时，会清理其 Engine 构图存储。

## 5. Graph Engine 内部 API

所有构图专用接口要求有效的 `X-iGraph-Service-Key` 和非空 `X-User-ID`。服务密钥由 `IGRAPH_CONTROL_SERVICE_KEY` 配置，不能依赖取消功能开关来决定是否发送此身份。BFF 和 Engine 必须能够以有效路径访问共享的 `IGRAPH_DATA_ROOT`；构图源文件必须位于该根目录内部。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/construction/capabilities` | 返回各格式 `algorithm_available`、支持的操作及 `schema` |
| `GET` | `/construction/{dataset_id}` | 未登记的数据集返回 `not_built`、`revision=0` |
| `POST` | `/construction/{dataset_id}/source` | 可信源登记：`source_path`、`source_filename` |
| `POST` | `/construction/{dataset_id}/build` | 可同时登记源；请求为 `source_path?`、`source_filename?`、`options` |
| `GET` | `/construction/{dataset_id}/graph` | 返回第 3 节图文档；无图时返回 `409 graph_not_ready` |
| `POST` | `/construction/{dataset_id}/preview` | 按 `base_revision` 校验完整操作批次，不写图 |
| `POST` | `/construction/{dataset_id}/commit` | 同样校验并原子保存新版本 |
| `DELETE` | `/construction/{dataset_id}` | 删除该用户、该数据集的构图结果和 Engine 保留源副本 |
| `POST` | `/sessions/{session_id}/graph/load_construction` | `{"dataset_id":"ds_people"}`，加载保存图并替换当前会话图 |

构图状态响应包含 `dataset_id`、`status`、`revision`、`node_count`、`edge_count`、`directed`、`source_filename`、`source_type`、`algorithm_available`、`message`、`updated_at` 和 `last_build_status`。

算法未接入的 build 返回 HTTP 200、`success=false`、`status=algorithm_unavailable` 和 `revision=0`。这是明确的业务状态，调用方不能只根据 HTTP 200 判断已生成图。算法抛错或输出不合法时返回 `status=failed`，不会保存半成品图。

预览和提交的公共响应结构为：

```json
{
  "success": true,
  "dataset_id": "ds_people",
  "status": "preview",
  "base_revision": 1,
  "revision": 2,
  "graph": {"directed": true, "nodes": [], "edges": []},
  "summary": {
    "before": {"node_count": 2, "edge_count": 1},
    "after": {"node_count": 0, "edge_count": 0},
    "changed_nodes": 2,
    "changed_edges": 1
  }
}
```

示例中的空图仅表示响应格式示例，生产不会生成示例图。预览返回的 `revision=base_revision+1` 是候选版本，磁盘版本不会因此改变；提交成功时 `status=ready`，该新版本才正式生效。

结构化错误通常为 `{"detail":{"code":"...","message":"..."}}`。批次操作错误还包含从 0 开始的 `operation_index`。

| HTTP / code | 处理方式 |
| --- | --- |
| `409 revision_conflict` | 重新读取最新图，再预览、确认；不能只改版本号强行重放 |
| `409 source_conflict` | 已构图 dataset 不能绑定不同内容或格式的源文件 |
| `409 graph_not_ready` / `source_not_registered` | 先处理源登记和构图状态 |
| `422 invalid_operation` / `property_conflict` / `invalid_graph` | 根据具体错误修改操作或算法输出 |
| `403 invalid_source` | 检查可信源位置、文件是否存在以及共享存储挂载 |
| `409 SESSION_EXECUTION_ACTIVE` | 当前会话处理完成后再切换加载图 |

## 6. 六类优化的精确操作

每项操作用 `op` 字段指定规则。操作按顺序作用于候选图，因此后项可以引用前项结果；任意一项失败都会拒绝整个批次。属性覆盖冲突、未知 ID、重复选择 ID、悬挂边、无效参数以及最终没有变化的批次均不会被静默保存。

| 操作 | 请求项 | 约束和属性处理 |
| --- | --- | --- |
| 节点转成边 | `{"op":"collapse_node","node_id":"bridge","edge_type":"CONNECTS"}` | 节点恰有两条相连边，无自环；有向图必须一入一出，压缩后连接两个不同邻居。新边用 `_collapsed_node`、`_collapsed_edges` 属性完整保留被压缩节点、两条边及其类型和属性，避免同名属性互相覆盖 |
| 节点属性转成边属性 | `{"op":"node_property_to_edge","node_id":"alice","property":"phone","edge_ids":["e1"],"target_property":"contact_phone","remove_from_node":true}` | 指定边必须与节点相连，源属性必须存在。目标边已有不同值时拒绝；`target_property` 默认为原属性名。`remove_from_node=false` 表示复制，`true` 表示移动 |
| 属性提取为独立节点 | `{"op":"extract_property_node","node_ids":["alice","bob"],"property":"phone","node_type":"Phone","edge_type":"HAS_PHONE","remove_from_node":true}` | 每个源节点必须包含该属性；根据新节点类型、属性名和值生成确定性 ID，相同内容复用同一属性节点。生成 ID 与不相容的现有内容冲突时拒绝；禁止提取到源节点自身 |
| 删除节点、边或属性 | `{"op":"delete","target":"node","ids":["alice"],"cascade":true}` | `target` 为 `node` 或 `edge`。提供 `property` 时只删除指定属性；不提供时删除实体。删除仍有相连边的节点必须显式 `cascade=true`，否则拒绝。删除边或属性时不接受级联 |
| 调整边方向 | `{"op":"reverse_edges","edge_ids":["e1"]}` | 仅适用于有向图；交换两端，保留边 ID、类型与属性 |
| 重命名类型 | `{"op":"rename_type","target":"node","old_type":"Person","new_type":"Customer"}` | 对全部匹配类型的节点或边生效。旧类型必须存在、新名称必须不同；不修改元素 ID 或业务属性 |

`remove_from_node` 的 Engine 默认值为 `true`，`cascade` 默认值为 `false`。前端提供对应复选框；Agent 应根据用户意图显式传递这些布尔值，避免依赖默认值推断删除范围。

## 7. 算法插件接入

入口是 `ConstructionStore.register_algorithm(source_type, algorithm)`，`source_type` 只能是 `sql`、`csv`、`txt`。插件为同步可调用对象：

```python
from pathlib import Path

def build_csv(source: Path, options: dict) -> dict:
    # TODO：由后续算法工作实现字段识别、实体/关系建模与稳定 ID 生成。
    # 返回第 3 节约定的内层 graph 对象。
    raise NotImplementedError("CSV 构图算法尚未实现")
```

服务装配示例：

```python
from graph_data_service.app import GraphDataService, create_app
from graph_data_service.construction import ConstructionStore

store = ConstructionStore()

# 只有真实算法完成并通过输出契约测试后，才启用相应注册语句。
# store.register_algorithm("csv", build_csv)
# store.register_algorithm("sql", build_sql)
# store.register_algorithm("txt", build_txt)

service = GraphDataService(construction_store=store)
app = create_app(service)
```

当前默认服务启动代码没有注册上述算法，也没有通过环境变量自动加载插件。后续接入需要在实际服务装配位置注入已注册的 store。

插件收到的是 Engine 已保留的源文件 `Path` 和独立的 `options` 副本。读取文件、确定节点/关系/属性、生成稳定 ID 都属于插件职责；SQL 文件不会在此框架中自动执行。算法必须返回完整合法图，框架校验成功后才发布第一版，任何异常或非法输出都不会留下可加载的半图。不要注册返回演示数据的占位算法，也不要注册只抛 `NotImplementedError` 的函数来把“算法可用”显示为真。

## 8. 持久化、版本与会话加载

Engine 构图存储布局：

```text
IGRAPH_DATA_ROOT/
  .construction_locks/<owner-and-dataset-hash>.lock
  users/<user_id>/datasets/<dataset_id>/construction/
    record.json
    source-<sha256>.csv
  users/<user_id>/graph_sessions/<session_id>/
    graph_versions.json
    dataset_binding.json
    snapshots/v001.npz
```

源副本扩展名随真实格式变化。`record.json` 包含保存图、当前 `revision`、源文件信息、校验和及状态，是构图事务的发布点。源文件复制和图记录写入使用临时文件及替换；同一 user/dataset 的操作通过线程锁和文件锁串行化，提交时再次检查 `base_revision`。一个并发提交成功后，基于旧版本的另一个提交必须返回冲突。

首次成功构图产生 `revision=1`；每次成功优化提交递增一次。预览、读取、会话加载不会增加 dataset 的 revision。数据集已为 `ready` 时再次 build 会返回已有图状态及 `reused=true`，不会重新执行算法。当前构图 store 保存最新版本，没有任意历史 revision 的查询或强制重建接口；会话自己的图版本历史仍按原有快照机制管理。

加载已保存图时，Engine 把节点/边 ID 映射到现有 GraphData 的名称和数组结构，同时在 `graph_metadata.construction_graph` 保留完整业务图文档。该元数据随 JSON/NPZ 和会话快照持久化；NetworkX 适配器提供业务属性及完整 `properties`，平行边使用 MultiGraph/MultiDiGraph 保留独立关系身份。

加载响应与 `get_graph_info` 包含 `construction_dataset_id`、`construction_revision`。它们与会话自己的 `graph_version` 是不同概念：前者标识 dataset 保存版本，后者标识本次会话图快照。优化提交只修改 dataset，不会在会话分析过程中偷偷替换图；用户选择使用、Agent 显式加载或后续 Run 检查时才加载保存版本。正在进行的分析保留原快照。

带 Run 身份的加载与提交沿用现有执行控制，转发 `X-Run-ID`、`X-Session-ID`、generation 和 operation 身份，并在提交边界检查取消信号。普通前端加载请求也会检查正在处理的会话作业。

**当前限制：构图会话使用本地 GraphData/NPZ 快照，即使部署同时配置了 Nebula。** 现有 Nebula 投影不能保证保留任意嵌套 JSON 属性，所以不会把它作为构图加载的属性存储替代品。需要 Nebula 原生构图处理时，应先实现并验证完整属性和关系身份的存储、物化及版本契约。部分旧图算法本身要求简单图；平行关系虽完整保存，但具体算子能否处理 MultiGraph 仍取决于该算子的现有支持。

## 9. MCP 与 Agent Runtime

构图工具目录由 `graph_engine/construction_catalog.py` 定义，并加入 Graph Engine 的公共工具目录。MCP 用专门 REST 转发调用同一 Engine 行为，不另做一套构图或优化实现。

| 工具 | 参数与行为 |
| --- | --- |
| `get_graph_construction_capabilities` | 无参数，读取算法可用性 |
| `get_graph_construction_status` | `dataset_id`，读取状态 |
| `get_constructed_graph` | `dataset_id`，读取真实元素及 revision |
| `load_constructed_graph` | `dataset_id`，加载到当前会话 |
| `preview_graph_optimization` | `dataset_id`、`base_revision`、`operations`，只预览 |
| `commit_graph_optimization` | 同上，通过既有工具确认流程后持久化 |

没有供 LLM 传入服务器路径的 build 工具；原文件构图由数据集面板发起。`list_datasets` 中的构图记录包含 `load_tool=load_constructed_graph`。通用 `load_dataset` 遇到构图记录会返回 `USE_CONSTRUCTED_GRAPH_LOADER`，避免绕过专用工具的范围检查和会话状态同步。

BFF 在 Run 的 `session_state.data_state` 中传入：

- `active_dataset_id`、`construction_status`、`construction_revision`、`source_format`。
- `construction_sources`：当前消息附件对应的 dataset ID、文件名、格式与状态，用于避免 Agent 误用另一个已活动图回答新附件的问题。
- `available_constructed_datasets`：当前用户及当前项目/个人范围内可用的已构图数据集。

Runtime 的 `construction_tool_hook` 对加载、预览和提交检查该会话范围，拒绝模型猜测其他项目的 dataset ID。成功加载后更新原生 Agno `session_state` 的活动 dataset 与 revision；提交本身不把保存动作冒充当前会话已加载。

Agent 应先读取真实图和版本，预览影响，再用相同操作及版本提交并经过工具确认。冲突需要重新读取、预览和确认；网络状态未知时先核对保存状态，不能盲目重试提交。保存后使用 `load_constructed_graph`、`get_graph_info`、`visualize_subgraph` 完成加载核验和展示。

## 10. 源码与验证入口

| 范围 | 主要文件 |
| --- | --- |
| 前端入口与工作台 | `frontend/src/main.tsx`、`frontend/src/construction.ts`、`frontend/src/components/GraphConstructionPanel.tsx`、`DatasetPanel.tsx`、`ProjectWorkspace.tsx`、`GraphWorkspace.tsx` |
| 前端请求和类型 | `frontend/src/api.ts`、`frontend/src/types.ts` |
| BFF 数据源与目录 | `backend/project_datasets.py`、`backend/construction.py`、`backend/app.py` |
| 构图与优化核心 | `graph_data_service/construction.py` |
| Engine API、会话与读取适配 | `graph_data_service/app.py`、`graph_data_service/adapters.py`、`graph_data_service/compute.py` |
| 工具契约与 MCP 转发 | `graph_engine/construction_catalog.py`、`graph_data_service/tool_catalog.py`、`mcp_service/graph_data_client.py`、`mcp_service/server.py` |
| Runtime 状态、指令和确认 | `agent_runtime/construction_state.py`、`agno_factory.py`、`instructions.py`、`approval_policy.py`、`resources/skills/graph_construction/SKILL.md` |

针对该功能的测试包括：

```bash
python -m pytest \
  tests/servers/test_graph_construction.py \
  tests/servers/test_graph_construction_http.py \
  tests/test_construction_api.py \
  tests/test_graph_construction_runtime.py \
  tests/test_construction_state_hook.py \
  tests/test_construction_native_hook.py \
  tests/test_construction_attachment_state.py
```

Engine 测试覆盖三种格式未实现状态、测试专用算法注入成功路径、源副本保留、重启读取、用户隔离、六种操作、属性冲突、非法批次不保存、并发 revision 冲突、HTTP 鉴权、session 快照加载、平行关系属性和可视化输出。成功路径中的小图仅用于自动化测试，不会被装入生产算法注册表。

前端测试入口为 `construction.api.test.ts`、`components/ConstructionEntrypoints.test.tsx` 和 `components/GraphConstructionPanel.test.tsx`；另应继续运行既有上传、图会话、数据集与 Runtime 回归，验证旧 CSV/TXT 已加载数据集的兼容性。

## 11. 本次验收（2026-09-07）

本地前端 TypeScript / Vite 构建成功；Vitest 全量 **166 项通过**。最后两次 CSS 调整针对桌面高度和移动端 flex 压缩，并在浏览器重新加载构建产物验证。

服务器使用独立目录 `/home/tanjiarui/igraph-construction-01a079d6`、Compose 项目 `igraph-construction`、专用网络及数据卷。复用现有各模块镜像作为依赖环境，源码挂载到独立容器；现有 `igraph`、`igraph-front` 服务未替换。测试服务不配置可用的模型供应商凭据。

| 服务器测试组 | 结果 |
| --- | --- |
| 构图 BFF、附件状态、项目数据集、Runtime 公共契约 | 39 通过 |
| 构图核心 / HTTP / session、图结构、既有图加载、项目资源 | 78 通过、1 跳过、17 子测试通过 |
| 真实 Agno / MCP / hook / 确认策略及工具模块 | 38 通过、17 子测试通过 |

真实 Agno 测试使用确定性测试 Model、真实 MCP entrypoint / ToolResult 包装和 SQLite，验证含/不含取消 hook 的成功、失败、跨范围拒绝三个分支，并关闭数据库重新打开确认状态持久化。测试没有请求真实模型。

下面命令在该服务器目录执行；`.test-deps` 是专用于验收的 pytest 安装目录。

```bash
docker run --rm --user 0 -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/work/.test-deps:/work:/work/tests \
  --entrypoint python igraph-front-backend:local -m pytest \
  tests/test_construction_api.py tests/test_construction_attachment_state.py \
  tests/test_projects.py tests/test_runtime_contracts.py -q

docker run --rm --user 0 -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/work/.test-deps:/work:/work/tests \
  --entrypoint python igraph-front-graph-engine:local -m pytest \
  tests/servers/test_graph_construction.py tests/servers/test_graph_construction_http.py \
  tests/servers/test_graph_structure.py tests/servers/test_graph_data_app.py \
  tests/servers/test_graph_data_json_load.py tests/test_project_resources.py -q

docker run --rm --user 0 -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/work/.test-deps:/work:/work/tests \
  --entrypoint python igraph-front-agent-runtime:local -m pytest \
  tests/test_construction_native_hook.py tests/test_construction_state_hook.py \
  tests/test_graph_construction_runtime.py tests/test_graph_tool_approval_policy.py \
  tests/servers/test_mcp_modules.py -q
```

额外尝试的全栈 `test_project_deletion.py` 因拆分镜像缺少对方模块依赖未完成收集（Backend 缺 SQLAlchemy，Runtime 缺 NumPy），不计入上述已通过测试；本次未宣称全仓库回归或真实模型训练已通过。

浏览器验收在 SSH 转发的 `http://127.0.0.1:22128` 完成：

- 数据集行内构图入口、SQL 算法待接入状态、禁止把原文件视为已构图。
- 明确标注的测试图执行“节点转成边”：8 节点 / 8 边预览为 7 / 7，保存版本由 1 升到 2；关闭工作台及重启服务后仍能读取。
- 两个新会话分别加载保存版本 2，保留电话号码等属性及压缩节点的溯源记录；服务本身的 CSV 算法仍为不可用。
- 1280×720 桌面、390×844 移动布局、浅色 / 深色主题，以及浏览器控制台无错误。

示例图仅在隔离验收数据卷中由测试专用 callable 创建，生产注册表没有任何 SQL/CSV/TXT 算法。真实语义转换仍需按第 7 节接入。

停止此验收栈（保留数据卷）可在服务器目录执行：

```bash
docker compose -p igraph-construction -f compose.yaml -f compose.test.yaml --env-file test.env stop
```
