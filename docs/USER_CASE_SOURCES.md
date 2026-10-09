# 预置示例的构图数据源

金融和《天龙八部》分别包含分析用 NPZ 快照以及对应原始构图数据源。金融三表保存在带表结构的 CSV bundle 中，小说保留完整 TXT；资源以 `source.gz` 压缩并由 manifest 的 SHA-256 校验。

Graph Engine 启动时安装图快照和原始数据源。首次读取个人数据集列表时，Backend 为该用户建立独立的 CSV/TXT 数据集，再通过经过用户与空间授权的内部接口写入已保存的预构图。此过程不调用大模型。两个数据集因此可在“创建你的图 → 我的构图数据源”中查看、重命名、优化和导入项目。

安装记录会保留：再次访问或重启不会新增重复数据集、覆盖用户修改或重新创建已删除的数据集。不同用户的原始文件与构图结果互相独立。新建任务页的示例卡片仍用于打开分析项目；左侧“新建任务”始终打开独立任务，项目内新建会话使用项目旁的加号。

重新生成预置图后，使用与 manifest 中源文件校验值一致的原始数据重新打包：

```powershell
python scripts/prepare_user_case_sources.py --finance-source F:/mypaper/data/finance/finance_csv --novel-source F:/mypaper/data/天龙八部.txt
```

部署时需要构建并启动 Graph Engine、Backend 和前端。共享卷保留既有项目与会话。
