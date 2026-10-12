# 分子硼化反应预置案例

本目录随服务镜像发布，启动自动安装 borylation-story.npz，无需网络下载、模型构图或本机交付目录。图的 SHA-256 见 manifest.json。重新启动不会覆盖用户已保存的图。

- raw：原作者五份 CSV，字节未修改。
- processed：八个底物的派生记录、BO-01 系统记录和结构化六问 workflow。
- graphs：八张分子图以及 BO-01 预存系统图。
- scripts：交付包的数据核对及转换脚本，仅供离线维护；日常部署不运行。
- background.md：绑定当前示例并验证图哈希后逐轮注入的背景。
- walkthrough.zh.md：交付包六问、证据和 PPT 讲稿。文中原评估实例路径属于历史交付环境，当前使用本项目 Compose。
- 前端编号化学结构图、产物图和原子坐标位于 frontend/public/examples/borylation。

数据作者：Mason Minot、Yannick Stenzhorn、Jens Wolfard、Sebastian Strobel、Philippe Jablonski、Daniel Zimmerli、Martin Binder、Uwe Grether、Rainer E. Martin、Alex Müller、David F. Nippa、Kenneth Atz、Gisbert Schneider。
论文：Advancing chemical reaction prediction in data-scarce drug discovery with active and geometric deep learning（2026）。
论文 DOI：https://doi.org/10.1038/s43588-026-01063-0
数据：https://zenodo.org/records/20773622 ，CC BY 4.0（https://creativecommons.org/licenses/by/4.0/）。
固定数据版本及原始文件 SHA-256：source_manifest.json。派生 JSON 和图增加案例 ID、原子映射及记录关联。
未附原作者 GPL 模型代码或权重；本包未运行其 GNN。当前结果为实验复盘和结构计算。
