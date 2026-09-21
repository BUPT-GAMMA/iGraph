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

当前仓库的 `.github/workflows/ci.yml` 检查应用代码，没有独立的文档站发布工作流。修改文档后需运行这里的构建与检查命令。若要接入自动发布，可另行配置 Cloudflare 凭据 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`，并将实际站点域名传给 `SITE_URL`。

The current `.github/workflows/ci.yml` checks application code; the repository has no separate docs publishing workflow. Run the build and check commands here after documentation changes. If adding automated publishing, configure `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`, and pass the actual site origin as `SITE_URL`.

## 内容与页面 / Content and pages

文档以「图驱动的数据分析智能体」为主线，介绍分析问题、图表示、工具计算证据与结果解释之间的联系。技能组织分析流程，自进化构图为后续分析准备和优化图表示。

The documentation presents iGraph as a graph-driven data analysis agent, connecting questions, graph representations, computed evidence, and interpretation. Skills organize analysis workflows; self-evolving construction prepares and optimizes representations for analysis.

- `content.json`：16 篇核心双语文档；`workspace.content.json` 为工作台操作指南，`construction-example.content.json` 为电商构图报告，`graph-tools.content.json` 为 72 项业务工具（含可选的报告工具）及 3 个会话管理接口的分类参考。核心章节包括源数据预览、构图流程、图算法、大图处理、图学习、接口与配置、记忆和技能。相同 slug 和 section ID 在语言切换时保持章节位置。
- `scripts/content.mjs`：组合 19 篇文档；`scripts/build.mjs` 生成 38 个文档页面、中英文封面、根入口、真实 404、Markdown、章节与工具搜索索引、llms.txt / llms-full.txt、可选 sitemap。
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

- 以代码中的工具目录、路由、配置和前端入口为依据。更新功能说明时同步中英文；历史实验保留日期与原始测量，不改写成当前版本的性能保证。
- 中文先说操作和用途，再解释实现。构图中的 workload 称为“评测查询集”，SQL oracle 称为“独立 SQL 参考执行器”，artifact 按上下文写成“分析结果”或“结果文件”；引用界面按钮时沿用界面原文。
- API 路径、参数、枚举、模型名和文件名保持原样，例如 `feature_source="embedding"`、`embedding.npy`。正文可解释“嵌入向量”，不能翻译配置值或文件名。
- 补充能力时写清入口、前提、结果和限制。区分源数据与当前图、数据集版本与会话图版本、训练指标与推理指标、报告内容与下载文件。

Use the tool catalog, routes, configuration, and UI as the source of truth. Keep factual changes aligned in both languages, preserve historical experiment dates and measurements, and never translate identifiers or literal configuration values. Describe how to access a capability, its prerequisites, its output, and its limits.
