# 模型服务与提供商管理

工作台右上角的「模型服务」和会话模型菜单中的「管理模型服务」打开同一个配置面板。提供商保存连接信息，模型保存型号、显示名称和能力；同一提供商下的模型共用 API 地址与密钥。

## 使用

1. 点击「添加提供商」，填写名称和类型后添加。支持智增增、OpenAI、DeepSeek、阿里云百炼、Google Gemini、Ollama 或自定义提供商；预设填入兼容接口地址，自定义提供商允许稍后填写地址。
2. 填写名称、API 密钥和 API 地址，保存连接。地址旁的信息图标可查看协议与实际请求地址。编辑已有提供商时点击标题旁的编辑按钮修改名称和类型。已保存的密钥不回传浏览器；留空保留，勾选清除则移除密钥。本地无认证服务可以不填密钥。
3. 「检测」请求提供商的模型列表，报告连通性和耗时。「管理」发现远程模型、搜索并批量添加；不支持模型发现的接口可以使用「添加」手动填写型号。
4. 在模型设置中修改显示名称、工具调用与视觉能力、上下文窗口及最大输出。导入后的能力和 Token 上限使用初始值，需要按提供商文档核对；远程模型列表不等于能力认证。图分析会话要求工具调用能力。
5. 模型右侧可以检测实际模型请求、设置新会话默认模型、启停、编辑和移除。会话菜单按提供商分组显示名称及型号；已有会话保留原选择，不因添加模型或改变默认值自动切换。

## 协议与部署

本实现沿用系统 Agent、图数据处理、取消与历史适配的 **OpenAI-compatible Chat Completions** 调用链。Google Gemini 使用官方 OpenAI 兼容地址，Ollama 使用 `/v1` 兼容接口。提供商名称用于连接管理，不会自动将原生 Anthropic Messages、Gemini generateContent 或 Responses 协议转换为 Chat Completions。

API 地址须包含实际的基础路径，例如 `https://api.example.com/v1`，系统追加 `/models` 或 `/chat/completions`；不自动猜测版本路径。不得将 iGraph 工作台地址填作模型接口。Docker 中访问本机 Ollama 时使用 `http://host.docker.internal:11434/v1`，并使 Ollama 可被容器网络访问。

模型服务沿用已有执行控制目录，需要所有服务启用 `IGRAPH_RUN_CANCEL_ENABLED`。原部署连接会迁移为真实提供商（如「智增增」），使用同一个配置面板。共享提供商由管理员修改密钥、地址和模型；普通账户可使用获授权的模型、设置默认和自己的显示开关。管理员添加提供商时可选择「仅自己」或「共享」，共享新模型供所有账户使用。未启用该功能的旧部署保留原有简单模型配置入口。

## 数据与兼容

- `igraph_shared_model_providers` 保存共享连接与模型定义，`igraph_provider_migrations` 记录一次性迁移。首次迁移按实际 API 地址与密钥分组识别提供商，保留原模型 ID、账户授权、默认与会话选择。迁移后以数据库配置为准，环境变量不再覆盖页面修改，也不会复活已删除的共享提供商或模型。旧部署原本不受此目录管理的服务仍使用各自配置。
- 新表 `igraph_model_providers` 按用户保存提供商连接、启用状态与版本。模型、版本、会话选择和默认偏好继续使用现有表；迁移为新增表，无需重建现有数据。
- 旧自定义模型按地址与密钥组合归入提供商，保留模型配置 ID、默认选择、会话选择和旧答案溯源。迁移幂等，删除标记阻止旧 JSON 配置被重新导入。
- 提供商修改和批量导入与模型目录共用事务锁。个人连接修改会在同一事务中生成关联模型的新版本；共享连接在各账户下次读取或任务开始前同步成版本，任务准入和共享修改共用 PostgreSQL 事务锁；在运行、等待审批或保存中的任务仍解析原快照中的连接版本。
- 提供商与模型编辑检查预期版本，过期页面收到冲突，不覆盖新配置。每批最多导入 200 个模型；重复型号跳过，批内失败整体回滚。界面自动分批导入更大的选择。
- 停用提供商会阻止其模型开始新请求，不取消已开始的任务。删除模型继续检查活动任务；点击标题旁的删除按钮并确认后，提供商、其下模型及凭据在同一事务内删除；共享提供商会检查所有账户的活动任务；任一模型正被活动任务使用时整批拒绝，其他模型不会被部分删除。
- 浏览器接口仅返回 `has_api_key`，密钥只通过已认证的内部服务接口供执行使用。凭据存储沿用现有数据库和模型版本机制；部署仍需保护数据库和内部服务认证边界。
- 模型发现不跟随重定向，限制总时长 20 秒、响应大小 4 MiB、最多展示 5,000 个去重型号，并将认证、配额、超时和不支持发现的错误转换为用户提示。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/model-providers` | 当前账户的提供商与连接预设 |
| `POST /api/model-providers` | 创建或按版本修改提供商 |
| `POST /api/model-providers/discover` | 使用已保存连接发现模型 |
| `POST /api/model-providers/import` | 按版本批量导入选中模型 |
| `DELETE /api/model-providers/{id}?revision=N` | 删除提供商及其模型 |
| `GET/POST /api/models` | 列出模型或保存模型参数 |
| `PATCH /api/models/state` | 按版本启停模型 |
| `PUT /api/models/default` | 设置新会话默认模型 |
| `POST /api/models/test` | 发送合成输入，检测模型与工具参数兼容性 |
| `DELETE /api/models/{id}?revision=N` | 移除模型 |

## 验证

自动化测试覆盖提供商与用户隔离、旧配置迁移、密钥保留和清除、配置版本冲突、并发修改、批量导入回滚与幂等、远程模型发现、异常响应、会话模型历史与前端操作。使用真实 PostgreSQL 的隔离 schema 验证事务锁。

浏览器及完整链路验收使用独立临时账户和 `tests/fixtures/model_verification_server.py` 合成服务，不调用外部付费模型。真实链路包括 Backend、Agent Runtime、MCP、Graph Engine、PostgreSQL 和 SSE，检查模型切换后图数据保留、历史传递、运行中的版本固定和不可用模型处理。第三方提供商的实际可用型号、额度和特定模型兼容性仍由其服务决定，可在界面逐一检测。

## 设计参考

借鉴 Cherry Studio 的提供商与模型分层、连接配置和发现后选择添加的交互，结合 iGraph 已有的模型快照、会话状态和工具调用机制实现。

- [智增增 API 地址](https://doc.zhizengzeng.com/doc-4296789)
- [Cherry Studio 自定义服务商](https://docs.cherry-studio-ai.com/pre-basic/providers/zi-ding-yi-fu-wu-shang)
- [Cherry Studio Provider & Model Registry](https://github.com/CherryHQ/cherry-studio/blob/main/docs/references/provider-model/provider-registry.md)
- [Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai)
- [阿里云百炼区域与 Base URL](https://help.aliyun.com/en/model-studio/base-url)
- [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)

界面与删除流程源码核对（Cherry Studio `fb1895c7`）：[ProviderHeader](https://github.com/CherryHQ/cherry-studio/blob/fb1895c7b76db616f40341ab1e78f2e69f4a5770/src/renderer/pages/settings/ProviderSettings/components/ProviderHeader.tsx)、[ProviderList](https://github.com/CherryHQ/cherry-studio/blob/fb1895c7b76db616f40341ab1e78f2e69f4a5770/src/renderer/pages/settings/ProviderSettings/ProviderList/ProviderList.tsx)、[ModelListItem](https://github.com/CherryHQ/cherry-studio/blob/fb1895c7b76db616f40341ab1e78f2e69f4a5770/src/renderer/pages/settings/ProviderSettings/ModelList/ModelListItem.tsx)。界面保留字段、名称和操作；协议细节按需查看，提供商删除通过集中确认完成。
