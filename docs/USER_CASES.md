# 预置图与示例问题

部署启动时，Graph Engine 将随源码提供的两个 NPZ 快照安装到持久化数据卷和服务器数据集目录。安装校验 SHA-256，重启不覆盖已保存的用户版本，也不调用模型重新构图。首页“使用预置图开始分析”可创建当前用户自己的示例项目；点击示例问题会选中对应图并开始提问。项目空会话也提供对应问题。

| 示例 | 数据来源 | 节点 | 关系 |
| --- | --- | ---: | ---: |
| 金融股权 | institution_main、individual_info、institution_shareholder_relationship 三张 CSV | 6,000 | 13,436 |
| 天龙八部人物关系 | 用户提供的完整《天龙八部》TXT，deepseek-v4-pro 处理 106/106 分块 | 304 | 791 |

小说预置资源位于 `graph_engine/resources/tianlongbabu/`，manifest 保存原始文件哈希、图快照哈希和完整分块处理记录。仓库只提供图与清单，不打包原始小说或凭据。图中人物名称按原文抽取，保留来源分块；当前没有自动合并“乔峰/萧峰”等别名，也不保证模型抽取的关系语义完全准确。金融快照中超额持股、覆盖不足和循环持股保留原始事实，由分析工具报告。

## 重新生成小说预置资源

在部署环境配置 `GRAPH_CONSTRUCTION_API_KEY` 和 `GRAPH_CONSTRUCTION_BASE_URL`（OpenAI 兼容基础地址）；这些值不会从构图请求的 options 接收。TXT 页面默认选择 `deepseek-v4-pro`，可选模型由 `GRAPH_CONSTRUCTION_MODELS` 限定。正文支持 UTF-8、GB18030/GBK 和带 BOM 的 UTF-16。

```sh
python scripts/prepare_novel_preset.py /path/to/天龙八部.txt --work-dir /path/to/private-checkpoints
```

脚本使用完整输入、12,000 token 分块、300 token 重叠和最多 3 路抽取；缓存绑定模型、完整提示和输出预算，可在失败后续跑。工作目录包含私有抽取结果，应保留在部署环境中，不提交到仓库。生成后重新构建服务器 Compose 镜像即可将快照随系统发布。

普通用户上传构图与内置快照安装是两个入口。普通构图先生成待保存草稿，可预览与优化；点击“保存构图结果”后，才可选择该图进入分析。
