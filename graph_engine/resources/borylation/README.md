# 分子硼化反应预置案例

本目录随服务镜像发布，启动自动安装 borylation-story.npz，无需网络下载、模型构图或本机交付目录。图的 SHA-256 见 manifest.json。首次升级会备份并迁移字节一致的旧版官方预存图；重新启动不会覆盖用户修改的图。

- raw：原作者五份 CSV，字节未修改。
- processed：八个底物的派生记录、BO-01 系统记录、预存结构描述符 chemistry_profile.json 与历史六问资料。
- graphs：八张分子原始图归档；系统只加载第一张 BO-01 的原子和化学键。产物、配体、溶剂及实验只保留为分析元数据，工作台不提供记录关系视图。
- scripts：交付包的数据核对及转换脚本，仅供离线维护；日常部署不运行。
- background.md：绑定当前示例并验证图哈希后逐轮注入的背景。
- walkthrough.zh.md：当前四问入口说明，以及历史六问、证据和 PPT 讲稿。文中原评估实例路径属于历史交付环境，当前使用本项目 Compose。
- 前端编号化学结构图、产物图和原子坐标位于 frontend/public/examples/borylation。

数据作者：Mason Minot、Yannick Stenzhorn、Jens Wolfard、Sebastian Strobel、Philippe Jablonski、Daniel Zimmerli、Martin Binder、Uwe Grether、Rainer E. Martin、Alex Müller、David F. Nippa、Kenneth Atz、Gisbert Schneider。
论文：Advancing chemical reaction prediction in data-scarce drug discovery with active and geometric deep learning（2026）。
论文 DOI：https://doi.org/10.1038/s43588-026-01063-0
数据：https://zenodo.org/records/20773622 ，CC BY 4.0（https://creativecommons.org/licenses/by/4.0/）。
固定数据版本及原始文件 SHA-256：source_manifest.json。派生 JSON 和图增加案例 ID、原子映射及记录关联。
未附原作者 GPL 模型代码或权重；本包未运行其 GNN。当前结果为实验复盘和结构计算。

## 当前四问与技能

默认问题由 backend/borylation_case.py 统一提供中英文版本：溶解性与毒性、环结构、标注芳香 C–H 候选、对称等价。用户完成一问后可用 Tab 填入下一问。延伸的 products / conditions / plan 工具步骤仍可使用。

运行时内置 agent_runtime/resources/skills/molecular-analysis-workflow/SKILL.md，包含方法选择、背景化学知识、证据分层与图上标注流程。结构描述符使用 requirements-data.txt 中固定版本 RDKit 离线生成，运行服务无 RDKit 依赖。维护时在仓库根执行 `PYTHONPATH=. python graph_engine/resources/borylation/scripts/build_chemistry_profile.py`；工具逐次校验描述符的结构指纹，不对修改过的分子沿用原描述符。

包内没有该分子的溶解度或毒性测定，也没有对应的预测模型结果。cLogP 与 TPSA 等属于结构计算描述符；反应溶剂、投料浓度和反应成败不构成溶解度或安全性证据。
