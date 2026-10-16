# 远程连接体验与查询结果展示：独立验收

验收时间：2026-09-27 20:30–20:33（Asia/Shanghai）。验收者为独立验收 agent，未参与本次实现。**A 项通过**：在下面明确的验证范围内，没有发现阻塞连接体验或查询结果卡片交付的问题。结论来自独立运行相关回归、真实页面只读操作、代码检查及逐项复核原始开发证据，不是直接采用开发阶段“测试绿色”的结论。

本报告只评价原需求第一、二部分。**B 项模型回答质量仍未解决，不在本报告通过结论之内。** 多数据库接入不在范围内。未修改产品代码、skill、模型或全局提示；未执行新的模型问答、数据库写入、连接保存/删除或会话绑定，没有新增产品核验机制、合并或推送。

## 版本与证据边界

- 对比区间：`3739a590aad87c64dd696b5d74b89272a1e569ca..ea952485f090385bcce51bded0d7d3209b38de28`。
- A 实现提交：`21f522c679e68627b931c7c1beeed2857be20a6a`；最终产品代码：`cd9700eb9871d8cade0dde856ddd51520a209e8b`。验收时 HEAD 为上述 `ea952485`，之后的交接文档不改变本次产品判定。
- 原需求：附件 `b54839f2-fd61-4976-bfca-6157a6d1ee40/pasted-text-1.txt` 第一、二部分。分项边界见[主交接](2026-09-27-remote-graph-ux-evidence.md)。
- 独立核对 4 个 A 产品文件的工作区、验收 HEAD 和运行容器内容，全部一致，见 [independent-runtime.json](../../output/remote-graph-test/ux-evidence/handoff/independent-runtime.json)。其余运行信息复核 [runtime.json](../../output/remote-graph-test/ux-evidence/handoff/runtime.json)，不把含既有未提交 Compose 配置的环境称为纯净提交镜像。
- 下表“回归重跑”指本 agent 运行已有测试，组件/API 依赖使用测试中既有 mock；“浏览器实测”指当前服务上的真实只读交互；“开发证据复核”不表示重新执行了其保存、绑定、查询或删除操作。

## 逐项验收

| 需求 | 独立检查与实际结果 | 状态及证据边界 |
| --- | --- | --- |
| 默认列表、空状态、独立新建/编辑 | 回归重跑覆盖空列表和返回列表；真实页面默认无表单，分别打开编辑、新建；只提供 Neo4j | 通过。空状态用回归验证，未清空真实账户连接 |
| 保存并使用 | 检查保存返回连接后调用当前会话绑定的实现；回归重跑覆盖保存及选择，开发浏览器用例覆盖真实新建 | 通过；本轮未重新保存真实连接 |
| 已保存未使用与绑定重试 | 回归重跑验证第一次绑定失败后保存仅一次、重试仅绑定原 ID；实现保留服务端 ID/revision、清空密码并锁定已保存表单。复核真实故障截图及 JSON：重试前后均 1 条，ID 均为 `f2294391-be86-4836-8434-60cd33a09fe8` | 通过；真实故障来自开发阶段对首次绑定 PUT 的一次网络中断注入，非自然故障，亦非本轮重演 |
| 独立测试、不隐式保存/选择、编辑使状态失效 | 回归重跑覆盖不保存、测试中/成功/失败、修改 URI 后成功提示消失；代码检查测试分支没有 `onSelect`，所有配置输入改变均清除状态 | 通过；本轮没有再次连接 Neo4j 执行测试请求 |
| 选择、解除、删除与本地图保留 | 回归重跑覆盖选择/解除、按版本删除、刷新恢复、本地图不被替换；复核开发真实选择/刷新/解除/删除用例与成功日志 | 通过；真实修改操作仅复核已有证据 |
| 地址、localhost、数据库及编辑密码解释 | 真实表单显示 URI 示例、服务端/容器 localhost 解释、数据库名称与显示名区别；真实编辑密码为空且展示保留凭据提示 | 通过；实测与截图、代码一致 |
| 错误分类、可用性与账号权限区分 | 回归重跑覆盖网络、认证、配置受控错误码及原始私密错误不展示；覆盖 unknown、not_read_only、verified_read_only 三种状态。代码未把非只读/未知权限作为禁用条件 | 通过；未分别人为制造真实认证、网络、配置故障。浏览器到应用的原始传输中断显示通用失败，另有“已保存，未使用”提示；数据库受控错误码有分类文案 |
| 账户切换清理与异步隔离 | 回归重跑覆盖列表已加载/等待/失败时切账号、旧响应迟到、保存后组件卸载、旧面板未卸载但账户已变；核对父组件 epoch/identity 防护和子组件 owner/alive 防护 | 通过；基于独立执行的回归及代码，未做真实双账户并发故障注入 |
| 运行中操作限制 | 回归重跑验证 busy 时新建、使用、编辑、删除、解除均禁用；代码检查表单输入/测试/保存同样受 busy/pending 限制，父组件传入运行门禁 | 通过；为避免新模型调用，未发起真实运行来复测 |
| 中英文、视觉、窄屏、键盘 | 当前页面以 1440×1000 和 390×844 验证；中英窄屏主操作滚动可达、页面无横向溢出。新建焦点进入名称；Tab/Shift+Tab 首尾循环；Escape 关闭。人工查看新生成的中文窄屏、英文桌面截图，间距/按钮层级与主题变量实现相符 | 通过；未扩展到所有设备、浏览器、暗色模式及屏幕阅读器 |
| 简洁结果、来源、默认折叠 | 当前真实历史会话恢复了 2 个证据卡片，技术详情都默认关闭，键盘 Enter 可展开；回归重跑覆盖最多 5 行预览及完整预览保留 | 通过；历史结果重放没有新模型/数据库查询 |
| 来源追踪、查询与内部标识 | 回归重跑及代码检查确认来源显示名/数据库在外，connection_id/revision/query_id、查询、参数和原始预览在详情；嵌套节点/关系摘要不暴露其内部 ID。真实历史详情可展开并显示标识；集成回归保留历史来源、不随连接改名替换 | 通过；技术折叠判定针对结果卡片，不承诺模型自由回答不会复述查询文本 |
| 采样、截断、缩短与错误不被折叠隐藏 | 回归重跑覆盖底层截断、模型预览截断、结构采样/截断、超过 5 行、无 source 错误；代码确认这些提示及长单元格缩短提示在 `details` 外 | 通过；少见错误/采样组合未在真实浏览器逐一制造，长单元格提示为代码复核 |

## 本轮执行记录

相关前端回归实际执行命令：

```bash
docker exec igraph-frontend-1 pnpm test src/components/RemoteGraphConnections.test.tsx src/components/RemoteGraphQueryResult.test.tsx src/remoteGraph.app.test.tsx
```

结果：**3 文件、31 测试通过**，耗时 4.11 秒；[完整日志](../../output/remote-graph-test/ux-evidence/handoff/independent-frontend-tests.log)。这证明所列状态回归，并不单独证明真实服务端行为或 B 项回答质量。

只读浏览器执行命令：

```bash
docker run --rm --network host \
  -v /home/<user>/iGraph:/home/<user>/iGraph \
  -v /home/<user>/.cache/ms-playwright:/ms-playwright \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
  mcr.microsoft.com/playwright:v1.63.0-noble \
  node /home/<user>/iGraph/output/remote-graph-test/ux-evidence/handoff/independent-browser.cjs
```

使用既有专用用户 `remote-graph-test-cora` 读取历史会话 `a60665c5-4c67-469b-abdd-e791cb72023a`。仅放行 GET/HEAD/OPTIONS、身份校验和已有会话激活；没有触发新增修改或模型请求。**17 个记录检查通过**，拦截请求列表为空。脚本是本次验收产物，不加入产品或常驻测试链路。

- [浏览器结果](../../output/remote-graph-test/ux-evidence/handoff/independent-browser.json)、[执行日志](../../output/remote-graph-test/ux-evidence/handoff/independent-browser.log)、[只读验收脚本](../../output/remote-graph-test/ux-evidence/handoff/independent-browser.cjs)。
- 本轮截图：[英文桌面](../../output/remote-graph-test/ux-evidence/handoff/independent-form-desktop-en.png)、[英文窄屏](../../output/remote-graph-test/ux-evidence/handoff/independent-form-mobile-en.png)、[中文窄屏](../../output/remote-graph-test/ux-evidence/handoff/independent-form-mobile-zh.png)。新截图均为用户名/密码为空的新建表单，没有填写或读取密码。
- 开发原始记录复核：[绑定重试](../../output/remote-graph-test/ux-evidence/browser-binding-retry.json)、[真实浏览器日志](../../output/remote-graph-test/ux-evidence/browser-live.log)、`frontend/integration/remote-graph.spec.ts` 的实际断言；人工查看[已保存未使用](../../output/remote-graph-test/ux-evidence/saved-not-used.png)和[默认结果](../../output/remote-graph-test/ux-evidence/query-collapsed.png)原截图。本轮未重跑该 E2E。
- 独立读取 Docker 状态，主项目 7 服务 running、5 个带健康检查服务 healthy；专用 Neo4j healthy。Frontend 与 graph-engine-worker 没有健康检查，未称其 healthy。前端 `http://127.0.0.1:21108`、后端 `http://127.0.0.1:21109/health` 本轮重新访问均 HTTP 200。

## 已知局限与交付判断

1. 未重跑真实创建/绑定/删除、完整前端构建或后端测试，也没有重演真实错误矩阵、运行中并发及双账户竞态。这些边界已有独立回归/代码检查或开发真实证据支持，不能写成“本轮全部端到端重验”。本次没有发现需要阻止 A 交付的待验项。
2. 后端 `X-User-ID` 信任边界沿用既有设计，前端账户隔离通过不等于完成整站认证防伪。没有进行渗透测试或全链路凭据审计；仅确认本次 UI/错误展示与空密码截图行为。
3. B 项中模型自由文本可出现未经核实的事实，也可能复述查询或技术字段；本报告只验收确定性结果卡片及连接操作。B 必须继续单独标为未解决。
4. 原有大 bundle 提示及本地开发配置差异沿用主交接记录。本次没有产品修改，故不为文档收尾重建或重启健康服务。
5. `output/` 产物被 Git 忽略，异机交接必须另行携带，提交 SHA 本身不包含这些浏览器截图与日志。

**独立结论：A 通过；没有记录到新的产品阻塞问题。B 不通过/未解决状态保持不变，不据此否决 A。**
