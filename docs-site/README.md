# iGraph 独立技术文档站 / Standalone documentation

这里只构建和部署 **iGraph 项目的技术文档网站**。不部署原工作台、Backend、Agent Runtime、Graph Engine 或数据库；不连接任何应用 API。

This directory builds and deploys **documentation about iGraph only**. It does not deploy the original workspace, Backend, Agent Runtime, Graph Engine, or databases, and it calls no application API.

## 预览 / Preview

需要 Node.js >=22.12.0。系统已有旧 Node 时，请先切换到新版本。

```sh
cd docs-site
npm ci
npm run build
npm run check
npm run dev
```

打开 [本地文档站封面](http://127.0.0.1:4173/)，默认显示英文；可在右上角切换中文。点击「Start reading / 开始阅读」进入文档。修改内容后重新 build 并刷新。也可用 `npm run preview` 验证 Wrangler 静态资产服务。

Open the [local docs cover](http://127.0.0.1:4173/) and select “Start reading” to enter the documentation. English is the default; the language control switches to Chinese and remembers your choice. Rebuild and refresh after editing content. `npm run preview` runs the Wrangler static asset server.

## 发布 / Publish

```sh
npx wrangler login
npx wrangler whoami
npm run deploy:check
npm run deploy
```

配置文件 `wrangler.jsonc` 的 `name` 默认为 `igraph-docs`，只发布 `dist`，成功后使用 Wrangler 返回的实际 `workers.dev` 地址。登录需要 Cloudflare 账号授权，不要把 API token 放入文档内容。

The default Worker name is `igraph-docs`. Only `dist` is published. Use the actual `workers.dev` URL returned by Wrangler after deployment. Login requires Cloudflare account authorization; never put tokens into documentation content.

获得真实地址后可设置 `SITE_URL` 生成 sitemap、canonical 和绝对 Markdown 链接：

```sh
# Bash — replace with the actual domain returned by Wrangler
SITE_URL=https://igraph-docs.YOUR_SUBDOMAIN.workers.dev npm run deploy
```

```powershell
# PowerShell
$env:SITE_URL = 'https://igraph-docs.YOUR_SUBDOMAIN.workers.dev'
npm run deploy
```

当前仓库没有 GitHub Actions CI 或文档站发布工作流。修改文档后需在本地运行这里的构建与检查命令。若要接入自动发布，可另行配置 Cloudflare 凭据 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`，并将实际站点域名传给 `SITE_URL`。

The repository has no GitHub Actions CI or docs publishing workflow. Run the build and check commands here locally after documentation changes. If adding automated publishing, configure `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`, and pass the actual site origin as `SITE_URL`.

## 内容与页面 / Content and pages

文档以「图驱动的数据分析智能体」为主线，介绍分析问题、图表示、工具计算证据与结果解释之间的联系。技能组织分析流程，自进化构图为后续分析准备和优化图表示。

The documentation presents iGraph as a graph-driven data analysis agent, connecting questions, graph representations, computed evidence, and interpretation. Skills organize analysis workflows; self-evolving construction prepares and optimizes representations for analysis.

- `content.json`：16 篇核心双语文档；`workspace.content.json` 为工作台操作指南，`construction-example.content.json` 为电商构图报告，`graph-tools.content.json` 为 72 项业务工具（含可选的报告工具）及 3 个会话管理接口的分类参考。核心章节包括源数据预览、构图流程、图算法、大图处理、图学习、接口与配置、记忆和技能。相同 slug 和 section ID 在语言切换时保持章节位置。
- `experiments.content.json`：GLM-5-turbo 基座下四类图分析实验的 SR，以及构图智能体相对 PK/FK 规则构图的成本改善。`scripts/experiment-tables.mjs` 生成 SR 分组表头并按列标注排名；`scripts/construction-results.mjs` 展示按数据集选择的构图结果。两类表格均导出 Markdown，源文件保留结果来源。
- `scripts/content.mjs`：组合 20 篇文档；`scripts/build.mjs` 生成 40 个文档页面、中英文封面、根入口、真实 404、Markdown、章节与工具搜索索引、llms.txt / llms-full.txt、可选 sitemap。
- `scripts/cover.mjs`：中英文封面文案、导航与静态点云后备图。根地址默认进入 `/en/`，仅在用户主动切换语言后记住所选语言；不按浏览器语言自动选择中文。直接访问 `/zh/` 或 `/en/` 保留对应语言，文档页标识链接返回对应语言的封面；关闭 JavaScript 时根页面仍为英文。
- `assets/cover.css`、`assets/cover.js`：封面布局和主题及语言切换。`assets/particles.js` 与 `assets/cloud-geometry.js` 绘制覆盖整页的密集三维星云，支持全页鼠标交互、设备性能适配和减少动态效果偏好；页面不可见时停止动画。
- `scripts/diagrams.mjs`：生成图驱动分析流程、系统架构、Graph Engine 执行、技能驱动的构图进化四种 SVG，每种提供中英文和深浅主题。
- `examples/`：人工构造的电商 SQL、离线构图参数和两轮真实测量摘要；构建器只复制这三个示例文件。
- `assets/`：三栏文档布局、深浅主题、移动端目录、本地全文搜索（Ctrl/Cmd+K、/）、复制按钮。
- `scripts/check.mjs`：语言完整性、内部链接/锚点、代码复制目标、搜索目标、JS 语法和 Cloudflare 静态文件限制检查。
- 页面无需浏览器 JavaScript 也能阅读和导航；封面此时显示静态点云。点云动画、搜索、复制和偏好保存需要 JavaScript。

All pages have complete server-independent HTML. Reading and navigation work without JavaScript; search, copy controls, and saved preferences require it. No remote fonts, analytics, CDN scripts, or backend requests are used.

Cloudflare 官方要求见 [静态资产](https://developers.cloudflare.com/workers/static-assets/) 与 [计费/限制](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)。

After editing content, run `npm run build` and `npm run check`, then preview the affected Chinese and English pages. To publish, verify the Cloudflare account and Worker name before running `npm run deploy`. A successful local build does not publish the site.

修改内容后，执行 `npm run build` 与 `npm run check`，并预览中英文页面。需要发布时，先核对 Cloudflare 账号和 Worker 名称，再运行 `npm run deploy`。本地构建成功不等于网站已经发布。

## 内容维护约定 / Editorial conventions

### 实验结果来源

2026-09-21 新增的 `experiments.content.json` 按用户提供的七张截图录入，用户确认所有 harness 的基座模型均为 GLM-5-turbo。图一、图二只取 GLM-5-turbo（对应 OpenClaw）和 iGraph 的 SR；图三作为分组表头参考；图四、图六只取 iGraph 的 SR，分别与图五、图七的 Hermes、Claude Code、OpenClaw 合并。图四、图六的 OpenClaw 数值与 harness 对比表不同，本次采用图五、图七，未混合或平均两组结果。每行的 `source` 保留截图编号与原始行名。原始 0–1 比例乘以 100，图五、图七已有百分数不再次换算。

`scripts/check.mjs` 检查四张表的基座模型、指标、数值范围、分组列数、来源记录，以及 HTML 和 Markdown 的最高分/次高分标记；并列成绩按不同分数排名。截图中的其他基座模型、Avg 和 TSA 均不录入结果页。

实验表格采用两层表头、独立的基座模型与指标栏，以及排名图例。容器宽度不超过 820px 时，通过 NAG／TAG／TPG 分组选项查看对应列；宽屏和打印显示完整对比表。分组切换使用原生单选控件与 CSS，支持键盘操作，无需 JavaScript；Markdown 导出始终包含全部结果。

构图实验由同日另外提供的两张表补充。第一张文件为 `92fa631ce10c72fa29f270bd586df627.png`，第二张为 `5b23269770d1c1857da18f6093db1693.png`。Amazon、AVS 取第一张的 27.33% / 53.20%、14.80% / 20.71%；MAG、StackExchange 取第二张的 11.79% / 19.91%、8.40% / 6.74%。每对数值依次为优化（搜索）查询成本下降、留出查询成本下降，基线均为 PK/FK 规则构图。所选结果在同一数据集的两项指标上均更优，不逐列拼接不同实验，也不跨数据集排序。

构图表的 `datasets[].results` 保留两组原始值及各自附带字段，`selectedSource` 明确选择来源。第一组的虚拟记录、保留列和候选数没有补到第二组；第二组的耗时也没有补到第一组。原表耗时与成本下降率不能简单等同，页面直接使用原表报告的成本下降率，不从耗时反算，也不编造未给出的基线绝对成本、模型配置或运行日期。构建检查验证所选整行确实在两项指标上均占优，HTML 与 Markdown 输出均包含对应结果。

### 文字与术语

- 以代码中的工具目录、路由、配置和前端入口为依据。更新功能说明时同步中英文；历史实验保留日期与原始测量，不改写成当前版本的性能保证。
- 中文先说操作和用途，再解释实现。构图中的 workload 称为“评测查询集”，SQL oracle 称为“独立 SQL 参考执行器”，artifact 按上下文写成“分析结果”或“结果文件”；引用界面按钮时沿用界面原文。
- API 路径、参数、枚举、模型名和文件名保持原样，例如 `feature_source="embedding"`、`embedding.npy`。正文可解释“嵌入向量”，不能翻译配置值或文件名。
- 补充能力时写清入口、前提、结果和限制。区分源数据与当前图、数据集版本与会话图版本、训练指标与推理指标、报告内容与下载文件。

Use the tool catalog, routes, configuration, and UI as the source of truth. Keep factual changes aligned in both languages, preserve historical experiment dates and measurements, and never translate identifiers or literal configuration values. Describe how to access a capability, its prerequisites, its output, and its limits.
