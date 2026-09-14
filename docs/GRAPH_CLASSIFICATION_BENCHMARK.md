# 图分类 Benchmark：远程运行与验收

本文说明 HIV/PCBA 图分类 benchmark 的输入契约、远程运行命令、指标语义和验收流程。
该 benchmark 是一次性命令行任务，不启动 Web 页面、登录服务、Agent、MCP、数据库或常驻
HTTP 服务。

## 1. 官方运行边界

正式结果只以远程 Linux 服务器上的 Conda `vlm` 环境为准。本地环境仅用于编辑代码，
不作为性能、正确性或复现性验收依据。

```bash
cd /path/to/iGraph-benchmark
conda activate vlm
```

启动脚本优先使用当前已激活的 `vlm`；未激活时会定位 Conda 并在脚本进程内自动激活
`vlm`。它不会执行 `pip install`、创建新环境或修改 CUDA/PyTorch 配置，并会原样保留
Python runner 的退出码。
运行前应确认数据和结果目录位于服务器本地磁盘，并预留足够的内存与空间。

## 2. 输入数据

runner 接受两个位置参数：数据集 profile 和 `.pt` 文件路径。

```bash
bash run_benchmark.sh <hiv|pcba> </absolute/path/to/dataset.pt> [options]
```

参考数据集信息：

| profile | SHA-256 | 图数 | train / val / test | 任务数 | 指标 |
| --- | --- | ---: | --- | ---: | --- |
| `hiv` | `680ccc639603c2230e4e61fb2e584cecb66c38b482a467be9227d9edd115b241` | 41,127 | 32,901 / 4,113 / 4,113 | 1 | ROC-AUC |
| `pcba` | `d863bfd9072ff6379392d9ed4c741cf258e45a599d19d2a1c057f134eac2b8a8` | 437,092 | 349,854 / 43,650 / 43,588 | 128 | macro AP |

HIV 的节点/边类别词表大小为 382/8。PCBA 为 232/11，包含 11 张无边图；PCBA 的
55,947,776 个标签位置中有 21,930,606 个 NaN。loader 会执行受限反序列化、全局类型检查、
逐图契约检查和 split 检查；profile、扩展名或数据结构不符合约定时直接失败。

可在远程服务器核对输入文件：

```bash
sha256sum /path/to/hiv.pt
sha256sum /path/to/pcba.pt
```

runner 默认把表中的 SHA-256 作为正式数据身份约束；结构正确但指纹不同的数据也会拒绝，避免
把其他数据误标为官方 HIV/PCBA 结果。数据文件不应提交到 Git。

## 3. 命令行参数

| 参数 | 默认值 | 约束与说明 |
| --- | --- | --- |
| `dataset` | 无 | 必填，`hiv` 或 `pcba` |
| `path` | 无 | 必填，存在且可读的 `.pt` 文件 |
| `--models` | HIV: `GCN GAT SAGE`；PCBA: `GCN` | 一个或多个 `GCN`、`GAT`、`SAGE` |
| `--epochs` | `1` | 正整数 |
| `--batch-size` | `256` | 正整数 |
| `--seed` | `42` | 整数随机种子，范围 `0..4294967295` |
| `--hidden-dim` | `8` | 正整数，同时用于节点嵌入、隐藏层和图嵌入 |
| `--num-layers` | `2` | 至少为 2；当前支持的 GNN backbone 不接受单层配置 |
| `--device` | `cpu` | `auto`、`cpu`、`cuda` 或 `cuda:N` |
| `--expected-sha256` | 官方 profile 指纹 | 使用自定义的 64 位十六进制 SHA-256 约束 |
| `--allow-fingerprint-mismatch` | 关闭 | 仅允许合成/自定义 smoke 数据绕过正式指纹约束 |
| `--output-dir` | `outputs/benchmarks` | JSON 结果目录，可使用绝对或相对路径 |

训练还固定使用 `lr=0.001`、`weight_decay=0`、`dropout=0`、`num_workers=0` 和
`global_mean_pool`。改变这些控制变量需要修改 runner，并应视为新的 benchmark 协议版本。

设备规则：

- `cpu` 保持历史 smoke benchmark 的可比性；
- `cuda` 使用默认 CUDA 设备；
- `cuda:N` 使用指定的非负设备索引；
- `auto` 根据运行环境解析出明确设备后，再将同一设备传给训练和推理；
- 请求的 CUDA 不可用或设备索引越界时，runner 直接失败，不静默回退到 CPU。

## 4. 标准运行命令

HIV CPU smoke：

```bash
bash run_benchmark.sh hiv /path/to/hiv.pt \
  --models GCN GAT SAGE \
  --epochs 1 \
  --batch-size 256 \
  --seed 42 \
  --hidden-dim 8 \
  --num-layers 2 \
  --device cpu \
  --output-dir /data/igraph-results
```

PCBA CPU smoke：

```bash
bash run_benchmark.sh pcba /path/to/pcba.pt \
  --models GCN \
  --epochs 1 \
  --batch-size 256 \
  --seed 42 \
  --hidden-dim 8 \
  --num-layers 2 \
  --device cpu \
  --output-dir /data/igraph-results
```

指定 GPU：

```bash
bash run_benchmark.sh hiv /path/to/hiv.pt \
  --models GCN \
  --epochs 1 \
  --device cuda:0 \
  --output-dir /data/igraph-results
```

不要在同一组横向结果中混用 CPU、不同 GPU 型号或 `auto` 解析出的不同设备。

## 5. 执行语义

一次命令按以下顺序执行：

1. 校验 CLI 参数、输入文件、结果目录和设备；
2. 创建隔离的内部 run/session 和临时 artifact 目录；
3. 加载并验证 `.pt` 数据，计算 fingerprint 并校验预期 SHA-256；
4. 依次训练选定模型，每个 epoch 完成 train 与 validation；
5. 按 validation 指标选择 best checkpoint；
6. 只在选优结束后对 test split 做完整评估，并拒绝空值、非有限或超出 `[0,1]` 的指标；
7. 对 test split 的第一个图执行一次推理，校验预测概率，并生成模型结构摘要；
8. 将完整结果原子写入输出目录，同时打印到标准输出；
9. 清理内部 session 和临时模型 artifact。

test 指标不得参与选优。PCBA 计算 macro AP 时，NaN 标签会被 mask；缺少正样本或负样本、
无法计算 AP 的任务会被标记并排除，而不是按 0 参与宏平均。

## 6. 输出与退出状态

默认结果路径：

```text
outputs/benchmarks/<dataset>-<UTC YYYYMMDDTHHMMSSZ>-<8hex>.json
```

runner 将严格、机器可读的完整 JSON 写到标准输出，把进度和最终结果路径写到标准错误；
`NaN`/`Infinity` 等非标准 JSON 值会导致测评失败。每份结果应
至少能够追溯以下信息：

- 代码 commit 与工作区状态；若服务器目录不含 `.git`，对应字段为空而不是中断测评；
- Python 可执行文件、真实 Conda 归属、PyTorch、PyG、CUDA、操作系统和实际设备；
- 输入路径、SHA-256/fingerprint 和数据集统计；
- 模型、训练配置、seed 与实际 device；
- validation/test 指标、best epoch、loss 和耗时；
- 可审计的单图预测和完整模型结构摘要；
- 顶层进程累计峰值内存，以及 CUDA 运行时逐模型的显存分配峰值。

成功时退出码为 0；参数错误为 2、环境/设备错误为 3、输入/数据指纹错误为 4、训练/验证错误
为 5、结果写入错误为 6。中断遵循 130/143。写入采用同目录临时文件原子替换，失败时不会留下
一份永久停在 `status=running` 的结果。

## 7. 远程验收

以下测试命令针对包含测试文件的源仓库；用 `scripts/build_benchmark_bundle.py` 生成的正式运行包
只保留测评运行时，不携带产品服务或测试代码。

### 7.1 Shell 与合成数据测试

这些检查应在远程服务器的 `vlm` 环境执行：

```bash
bash -n run_benchmark.sh

python -m unittest discover -s tests -p 'test_benchmark_runner.py' -v
python -m unittest discover -s tests -p 'test_benchmark_bundle.py' -v

for TEST_FILE in \
  test_graph_classification_batching.py \
  test_graph_classification_dataset_loader.py \
  test_graph_classification_metrics.py \
  test_graph_classification_train_infer.py \
  test_graph_classifier.py \
  test_graph_ml_dataset_store.py; do
  python -m unittest discover -s tests/servers -p "$TEST_FILE" -v
done
```

### 7.2 真实数据 loader 统计

```bash
GRAPH_ML_TEST_HIV_PT=/path/to/hiv.pt \
GRAPH_ML_TEST_PCBA_PT=/path/to/pcba.pt \
python -m unittest discover -s tests/servers \
  -p 'test_graph_classification_real_data.py' -v
```

不设置训练开关时，真实训练测试会跳过，只核验完整数据加载与统计。

### 7.3 真实数据 1 epoch 训练

```bash
GRAPH_ML_TEST_HIV_PT=/path/to/hiv.pt \
GRAPH_ML_RUN_REAL_HIV_TRAINING=1 \
python -m unittest discover -s tests/servers \
  -p 'test_graph_classification_real_data.py' -v

GRAPH_ML_TEST_PCBA_PT=/path/to/pcba.pt \
GRAPH_ML_RUN_REAL_PCBA_TRAINING=1 \
python -m unittest discover -s tests/servers \
  -p 'test_graph_classification_real_data.py' -v
```

最后再分别执行第 4 节的 HIV、PCBA shell 命令，确认输出目录各新增一个完整 JSON，命令退出码
为 0，结果中的 fingerprint、数据统计、device、配置与实际运行一致。

## 8. 结果可比性

- 正式实验必须使用干净、明确的代码 commit；工作区有改动时结果只能作为调试记录；
- 同组实验固定数据文件 hash、Conda 环境、硬件、device、模型配置和 seed；
- 首次运行可能受文件系统缓存影响，性能对比应采用一致的预热与重复策略；
- 1 epoch、小隐藏维度结果只用于链路 smoke，不代表论文级最终精度；
- 不应跨硬件直接比较耗时或内存，也不应把 CPU 历史数据当作 GPU 基线；
- JSON 结果是正式记录，终端截图或手工抄写表格不能替代原始结果。
