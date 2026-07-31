# 图分类真实数据 Benchmark

本文记录随需求提供的 HIV/PCBA 数据在图分类 baseline 上的真实联调结果。这里的 1 epoch 结果用于验证完整数据路径、指标和资源边界，不代表经过调参的论文级最终精度。

## 数据与运行环境

- 基准代码：`179b657ccdf0796747cb1d87912f0ba18d559722` 加 Step 9–Step 12 未提交工作区；收尾提交后应以新 commit 重跑正式长期 benchmark。
- 运行日期：2026-07-31。
- 系统：macOS 15.4.1，arm64。
- PyTorch：2.12.1；PyG：2.8.0。
- 设备：CPU；当前环境无 CUDA、无可用 MPS。
- seed：42。
- 模型公共配置：`node_embedding_dim=8`、`hidden_dim=8`、`embedding_dim=8`、`num_layers=2`、`dropout=0`。
- 训练公共配置：1 epoch、`lr=0.001`、`weight_decay=0`、`batch_size=256`、`num_workers=0`。
- 第一版所有模型固定 `global_mean_pool`，保留 `xe` 但不把边类别送入模型。

数据文件不会加入 Git：

| 数据集 | SHA-256 | 图数 | 划分 train/val/test | 任务数 | 指标 | 缺失标签率 |
| --- | --- | ---: | --- | ---: | --- | ---: |
| HIV | `680ccc639603c2230e4e61fb2e584cecb66c38b482a467be9227d9edd115b241` | 41,127 | 32,901 / 4,113 / 4,113 | 1 | ROC-AUC | 0 |
| PCBA | `d863bfd9072ff6379392d9ed4c741cf258e45a599d19d2a1c057f134eac2b8a8` | 437,092 | 349,854 / 43,650 / 43,588 | 128 | macro AP | 0.3919835169 |

HIV 的节点/边类别词表大小为 382/8。PCBA 为 232/11，含 11 张无边图；55,947,776 个标签位置中有 21,930,606 个 NaN。Loader 对上述数据完成了静态全局类型检查、安全反序列化、逐图契约校验和 split 校验。

## 1 epoch 联调结果

所有行都走 `GraphDataService.dispatch_tool()` 的 `load_graph_classification_dataset → train → infer → visualize_model_architecture` 公开工具分发路径。训练耗时包含完整 train epoch、完整 validation 指标和完整 test 指标，不包含数据加载。

| 数据集 | 模型 | val 指标 | test 指标 | train loss | test loss | 完整训练评估耗时 | 进程峰值内存 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| HIV | GCN | ROC-AUC 0.483836 | ROC-AUC 0.484328 | 0.637866 | 0.372735 | 0.90 s | 2.57 GB |
| HIV | GAT | ROC-AUC 0.562837 | ROC-AUC 0.531826 | 0.422283 | 0.159040 | 1.51 s | 2.58 GB |
| HIV | SAGE | ROC-AUC 0.626090 | ROC-AUC 0.579302 | 0.373099 | 0.145550 | 0.75 s | 2.58 GB |
| PCBA | GCN | AP 0.035814 | AP 0.036034 | 0.100227 | 0.066999 | 10.91 s | 6.19 GB |

HIV 加载耗时 8.44 秒；PCBA 加载耗时 95.79 秒。PCBA 的 validation 和 test 各有 127 个可计算 AP 的任务；另一个任务因划分中缺少正样本或负样本而按约定跳过，未把无效任务写成 0 后参与宏平均。所有指标均为有限值，test 只在按 validation 选回 best checkpoint 后执行。

使用相同 CPU、seed=42 和相同配置分别复跑 HIV GCN 与 PCBA GCN 后，train loss、test loss、validation 指标和 test 指标均与表中首次运行逐值一致；模型 ID 和运行耗时按预期不同。

## 复现命令

真实统计集成测试默认跳过，显式传入路径时才运行：

```bash
GRAPH_ML_TEST_HIV_PT=/path/to/hiv.pt \
GRAPH_ML_TEST_PCBA_PT=/path/to/pcba.pt \
.venv/bin/python -m unittest discover -s tests/servers \
  -p 'test_graph_classification_real_data.py' -v
```

显式开启真实训练测试：

```bash
GRAPH_ML_TEST_HIV_PT=/path/to/hiv.pt \
GRAPH_ML_RUN_REAL_HIV_TRAINING=1 \
.venv/bin/python -m unittest discover -s tests/servers \
  -p 'test_graph_classification_real_data.py' -v

GRAPH_ML_TEST_PCBA_PT=/path/to/pcba.pt \
GRAPH_ML_RUN_REAL_PCBA_TRAINING=1 \
.venv/bin/python -m unittest discover -s tests/servers \
  -p 'test_graph_classification_real_data.py' -v
```

输出完整 JSON benchmark 记录：

```bash
.venv/bin/python scripts/benchmark_graph_classification.py \
  hiv /path/to/hiv.pt --models GCN GAT SAGE \
  --epochs 1 --batch-size 256 --seed 42 --hidden-dim 8 --num-layers 2

.venv/bin/python scripts/benchmark_graph_classification.py \
  pcba /path/to/pcba.pt --models GCN \
  --epochs 1 --batch-size 256 --seed 42 --hidden-dim 8 --num-layers 2
```

脚本在临时目录存放会话与模型 artifact，退出时自动清理；标准输出包含 commit、工作区状态、dataset fingerprint、完整配置、best epoch、指标、耗时和峰值内存。需要保存结果时应重定向到 Git 仓库外部。

## 验收结论与边界

- HIV 的 GCN、GAT、SAGE 均完成真实全数据训练、validation/test ROC-AUC、artifact 保存、安全 checkpoint 恢复、指定图推理和 Mermaid 架构生成。
- PCBA 的 GCN 完成 349,854 张训练图的完整 epoch、43,650 张 validation 图和 43,588 张 test 图的完整 AP 评估，并验证 128 任务响应截断路径。
- GAT/SAGE 的 PCBA 代码路径由合成 128 任务数据测试覆盖；首版验收按计划只要求至少一种 backbone 完成 PCBA 真实全数据运行。
- 当前性能记录是 CPU 小模型 smoke benchmark。正式精度 benchmark 应在收尾 commit 后固定硬件和更合理的 epoch/隐藏维度重跑，不应把本表当作模型效果上限。
