# 模型管理与会话切换改造

> 提供商管理、模型发现与新版界面已实现，使用说明与兼容边界见 [模型服务与提供商管理](../guides/MODEL_PROVIDERS.md)。

## 目标

每个会话独立保存模型选择；选择只影响下一轮。运行启动后固定配置版本，暂停审批及停止保存期间不允许切换。模型停用或删除不影响历史查看，不静默回退到其他供应商。

## 实施与验收

1. 统一模型配置标识、版本、持久化和旧配置迁移。
2. 将模型切换和运行准入纳入同一数据库锁，记录本轮配置快照。
3. 实现停用、删除、默认模型和失效后的会话恢复。
4. 展示所有可用型号，提供独立管理入口及连接测试。
5. 适配跨模型历史、能力限制和上下文预算，记录实际模型。
6. 验证并发、刷新、服务重启、审批继续、模型失效、中英文与窄屏；确认图谱及训练不被重新执行。

每阶段完成相关验证、重建受影响的 Compose 服务并单独提交。

## 使用规则

顶部「模型管理」可添加、编辑、测试、停用和删除自定义接入配置，也可设置默认模型。输入框中的选择器展示全部型号，支持搜索和最近使用。保存配置不会自动切换当前会话；默认模型只影响新会话。

同一上游型号可以配置多个接入地址，系统用独立配置 ID 区分。会话选择和配置内容分别记录版本：浏览器拿着旧版本修改时会要求刷新，避免两个标签页相互覆盖。模型切换不会重载图谱、清空聊天记录或重新启动训练。

每轮开始时固定模型配置版本。运行、等待审批、停止保存期间不能切换会话模型；编辑或停用配置不改变已经开始的一轮，审批继续也使用原版本。有运行仍在使用某配置时不能删除，界面会列出涉及的会话。运行结束后允许删除，旧消息仍显示当时实际使用的模型。

停用、删除或缺少接入配置时，会话仍可查看历史，但不能继续发送；选择另一个可用模型即可恢复。失效的默认模型也需要用户重新选择，系统不会偷偷换到另一家供应商。

## 部署与存储

- 启用 `IGRAPH_RUN_CANCEL_ENABLED=true`，并让相关服务共享有效的 `IGRAPH_CONTROL_SERVICE_KEY`。模型版本、运行准入和取消状态使用同一套控制机制。
- 模型目录、版本、会话选择和默认偏好保存在 Agent Runtime 数据库的 `igraph_model_*` 表中。首次访问自动迁移旧自定义配置；删除记录保留无密钥的标记，避免旧文件将其重新导入。
- 列表接口只返回密钥配置状态；可编辑连接通过带身份和版本校验的 no-store 凭据接口读取真实密钥。它们仍是数据库中的敏感明文，部署者需要保护数据库及备份；旧版本为运行恢复保留，删除配置时一并移除其版本密钥。
- 原部署模型迁移到共享提供商，管理员在模型服务界面配置连接、管理与删除模型；普通账户保留原授权、默认选择及个人启停。迁移后配置保存到数据库，不再被环境变量覆盖。提供商独立保存接口类型：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 或 Gemini GenerateContent。厂商预设填入默认类型和地址，用户可在“接口类型”中修改；旧记录缺少类型时继续使用 Chat Completions。
- Docker 内接入宿主机服务时，应填写容器可达地址，例如 macOS 的 `http://host.docker.internal:端口/v1`，不能填写指向容器自身的 `localhost`。

## 历史与能力检查

切换后保留用户可见历史。发送请求前复制并规范化历史中的工具调用及结果，移除不兼容的供应商字段；中断或不成对的工具记录转成明确标注的历史文本，不重新执行。无法支持的媒体输入会明确报错。

模型配置中的上下文窗口和输出上限必须填写接入端点的真实值。内置模型通过 `IGRAPH_MODEL_CONTEXT_WINDOW` 配置，默认 131072；新建自定义配置同样默认 131072，但这不代表端点一定支持该容量。当前完整图工具集的定义本身较大，32k 配置可能连系统指令和工具定义都无法容纳，此时会阻止发送并提示容量不足。

历史过长时按完整轮次裁减请求，并附上有长度上限的摘录摘要，界面标记已压缩；原始聊天记录不被修改。摘要并不保留全部细节。预算采用跨供应商估算而非精确 tokenizer，仍可能收到供应商的容量错误；系统不通过静默截断当前问题或换模型来掩盖错误。

文本格式识别、文本构图使用本轮冻结的接入配置。运行前上传触发识别时使用会话当前选择。后台任务只传运行身份，由服务间私有接口读取对应配置，不把密钥写进任务载荷。

「测试连接」只发送合成的短消息及最小工具定义，不发送聊天或图数据；它可能产生少量费用，也不能证明模型的分析质量、所有工具能力或完整上下文容量。

## 2026-09-20 验收

最终自动化结果：前端 53 个文件、483 项测试通过，生产构建通过；模型专项 86 项、取消与原生持久化 106 项、BFF/取消控制 17 项、隔离计算 25 项测试均通过。另完成 24 次真实 PostgreSQL 并发竞争验证及 Compose 整链路验收。现存告警为依赖弃用提示及前端包体积提示。

| 场景 | 结果 |
| --- | --- |
| 新增 B 时当前会话仍使用 A；默认模型只影响新会话 | 通过 |
| A → B 后带上原有历史，历史答案仍标明各自实际模型 | 通过，包含页面刷新和服务重启 |
| 两个标签页使用旧选择版本提交 | 拒绝冲突，避免覆盖 |
| 运行中切换、删除正在使用的配置 | 拒绝；停止、等待审批及恢复协议有回归覆盖 |
| 运行中编辑或停用配置 | 原轮继续使用旧快照；下一轮按新状态检查 |
| 删除模型后查看历史，重新选择后继续 | 通过；不会静默回退 |
| 切换模型后图数据及图版本 | 保持一致，使用独立的 3 节点测试图核对 |
| 上下文过长、不支持媒体、历史工具调用中断 | 明确提示或规范化历史；原记录保留 |
| PostgreSQL 中切换与发送同时发生 | 24 次竞争测试通过，结果符合原子准入规则 |
| 中英文、390px 窄屏、深浅主题、键盘操作 | Playwright 浏览器验证及前端组件测试通过 |

模型专项回归命令：

```sh
PYTHONDONTWRITEBYTECODE=1 IGRAPH_RUN_CANCEL_ENABLED=false .venv/bin/python -m pytest \
  tests/test_model_catalog.py tests/test_model_api.py tests/test_model_history.py \
  tests/test_custom_models.py tests/test_execution_store.py \
  tests/test_run_model_gateway.py tests/test_provider_gateway.py -q -p no:cacheprovider
```

前端运行 `npm test -- --run` 和 `npm run build`（目录 `frontend`）。取消整链路回归运行 `tests/test_cancellation_http.py`，默认对首 token、流式输出、训练及审批各重复 20 次；原生持久化/取消协议由 `tests/test_native_persistence.py`、`tests/test_native_cancellation_contract.py` 覆盖。

真实 PostgreSQL 锁测试脚本为 `tests/fixtures/model_postgres_verification.py`，在 Runtime 容器内以 `PYTHONPATH=/app` 运行；它新建唯一测试 schema 并在结束时删除，不使用业务会话。

Compose 整链路验收使用 `tests/fixtures/model_verification_server.py` 作为本地合成 OpenAI-compatible 端点。用独立的 `model-verification-*` 测试账号添加 Verification A/B，分别接入 `http://host.docker.internal:21119/a/v1` 和 `/b/v1`，上游型号填 `model-a` / `model-b`，窗口 131072。先在 A 会话生成一条合成回复，再运行 `tests/fixtures/model_full_stack_verification.py USER SESSION`。脚本会更改、停用和删除 B，创建测试会话及小型数据集；浏览器核验后删除专用测试账号。

验收未调用真实商业模型，没有验证每家供应商的模型质量和协议差异。交付环境已重建受影响的 Compose 服务，访问地址为 `http://localhost:21108/`；配置了健康检查的服务均健康，独立 Worker 进程运行正常。

专用测试账号、合成会话/数据集和模拟模型凭据已清理。浏览器截图及记录保留在被 Git 忽略的 `output/playwright/` 中。

## 原生协议与提供商选择

- 选择器参考 [Cherry Studio Combobox](https://github.com/CherryHQ/cherry-studio/blob/main/packages/ui/src/components/primitives/combobox.tsx)，使用 Radix Popover 与 cmdk，支持厂商图标、搜索、方向键、Enter 和 Escape。图标来自 MIT 许可的 [LobeHub Icons](https://github.com/lobehub/lobe-icons)。
- 地址按接口类型统一解析。OpenAI 输入包含版本的基础地址；Anthropic 可输入根地址或 `/v1`；Gemini 可输入根地址、`/v1beta` 或 `/v1`。反向代理的路径前缀保留。
- 发现模型使用对应鉴权和 `/models` 格式；Anthropic、Gemini 支持游标分页。Gemini 过滤不支持 generateContent 的模型。总请求限制 20 秒、4 MiB、50 页，列表最多 5,000 项，重复游标会报错。模型检测发送合成文本及工具定义到实际生成端点，不发送会话内容。
- Agent 使用 Agno 2.9.0 的原生 OpenAIResponses、Claude、Gemini 适配器，保留既有工具 DAG 和运行取消。Responses 使用 `store=false`，保留工具 call_id 与加密推理；Gemini 保留当前连接的 thought_signature。跨连接历史中的签名不会转发，旧工具结果以历史文本保留。构图文本抽取使用同协议的官方 SDK。
- 当前支持 API Key 接入；Azure 部署、Vertex AI 服务账户、Ollama 原生接口未列入类型选项。Ollama 通过其 OpenAI 兼容接口接入。

协议契约依据：[OpenAI Responses 迁移指南](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Anthropic 模型分页接口](https://platform.claude.com/docs/en/api/models/list)、[Gemini Models](https://ai.google.dev/api/models)。`tests/test_native_model_protocols.py` 通过真实 SDK 和模拟 HTTP 验证文本、流式工具往返、取消关闭、构图调用；无需商业 API 密钥。
