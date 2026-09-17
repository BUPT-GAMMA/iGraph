# 数据集接口与客户端迁移

当前保留旧 HTTP 客户端的请求、响应体、状态码及数据行为，尚未安排删除日期。
成功响应增加 `X-iGraph-API-Deprecated: true` 和
`X-iGraph-Migration-Guide: /api/dataset-api-migration`；OpenAPI 中对应操作标为 deprecated。
登录用户可通过 `GET /api/dataset-api-migration` 获取结构化说明。

## 当前入口和调用方

| 入口 | 调用方和职责 | 状态 |
| --- | --- | --- |
| `/api/personal-datasets` | 当前 React 页面，个人上传数据集目录 | 继续维护 |
| `/api/projects/{project_id}/datasets` | 当前 React 页面，项目上传数据集目录 | 继续维护 |
| `PATCH /api/sessions/{session_id}/dataset` | 页面选择当前会话数据集，校验归属并同步选择状态 | 继续维护 |
| `GET /api/datasets` | 原有 Registry 目录及授权的个人构图结果 | 保留兼容并提示迁移 |
| `DELETE /api/datasets/{dataset_id}` | 原有 Registry 删除行为 | 保留兼容并提示迁移 |
| `POST /api/sessions/{session_id}/load-dataset`、`save-dataset` | 旧 HTTP 客户端的 Registry 加载、保存 | 保留兼容并提示迁移 |
| MCP `list_datasets`、`load_dataset`、`save_as_dataset` | 当前 Agent 的数据集工具 | 继续维护 |
| MCP `load_constructed_graph` | 加载保存的构图版本并同步会话选择 | 继续维护 |

`frontend/src/api.ts` 使用当前页面接口；`frontend/public/index.html` 中保留的旧页面代码
仍引用原有 HTTP 入口。外部脚本的使用情况尚未确认，因此继续保留兼容。
MCP 工具调用 Graph Engine 的工具接口，其维护状态独立于 BFF HTTP 入口的弃用标记。

## 可以直接迁移的构图路径别名

个人构图入口统一使用 `/api/personal-datasets/{dataset_id}/construction`，
其 `/build`、`/graph`、`/preview`、`/commit` 子路径保持不变。
原 `/api/datasets/{dataset_id}/construction` 系列是相同数据的路径别名；
可保留数据集 ID、请求方法和请求体，只替换路径前缀。
项目构图继续使用 `/api/projects/{project_id}/datasets/{dataset_id}/construction`。

## Registry 与个人／项目目录的迁移边界

Backend 的 `ProjectDatasetDirectory` 保存上传源文件及个人／项目归属；
Graph Engine 的 `DatasetRegistry` 保存公共／账户级图、历史版本和可能的 Nebula 引用。
Agent 保存的数据集仍可能只存在于 Registry。两个目录的 ID 和加载、删除语义不同，
因此旧 Registry HTTP 入口保持原行为，迁移提示不自动重定向请求。

完整迁移应按以下顺序实施：

1. 盘点旧客户端和 Registry 数据，包括公共样例、账户数据、历史版本、会话绑定与 Nebula 引用。
2. 明确每份数据的新归属，建立可追溯的新旧 ID 映射，保留源数据和历史版本。
3. 为页面和 Agent 接入统一的数据集管理逻辑，验证权限隔离、加载、保存及删除。
4. 迁移调用方，确认旧入口已无调用后再安排下线日期。

本次兼容标记不搬迁数据文件或改写数据集 ID。

## 部署要求

四个服务必须配置相同的非空 `IGRAPH_CONTROL_SERVICE_KEY`，即使取消功能关闭。
页面读取个人／项目列表不经过 Engine 的服务密钥检查；完整部署仍统一要求该配置，
使 Agent 和内部数据集访问在启动时具备所需凭据。生成及配置步骤见 [项目部署](../README.md#部署)。
