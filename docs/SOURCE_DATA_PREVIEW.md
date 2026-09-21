# 构图前的源数据示例

在“创建你的图”上传数据源、打开构图工作台后，“源数据示例”可在构图前、构图中和构图完成后使用。无需先启动模型或生成图。

构图前默认展开源数据示例；图就绪后自动收起，为图画布保留空间。点击“源数据示例”可随时重新展开查看。

- **SQL**：选择源表，查看前 5 条记录、字段类型、主键、外键及字段含义。支持显式 `CREATE TABLE` / 字面值 `INSERT INTO ... VALUES` 的文本预览，以及以 `.sql` 上传的 SQLite 数据库镜像。其他 SQL 语句会跳过并提示，不会执行上传脚本。
- **CSV**：显示表头与前 5 条完整记录；支持单文件、配置过主外键的多表 CSV 数据包及界面中的分隔符选择。中文、带引号的分隔符和多行单元格按 CSV 规则解析。
- **TXT**：保留换行显示开头最多 4,000 个字符，标注截断。预览不会修改源文件；文本构图仍由已有的自动、逐行或 LLM 实体关系抽取模式处理。

字段含义依次采用源文件注释、主外键结构事实、常见字段名提示。名称提示显式标为“推断 · 待核对”，不能当作业务数据字典；无法确定时显示未提供说明。字段名不能证明单位、时区、状态编码含义或计数覆盖范围。

示例保留行与列的对应关系，区分 `NULL`、空字符串、缺失值、`0` 和 `false`。长单元格截断到 300 个字符；超出 JavaScript 精确整数范围的标识符以字符串展示，避免浏览器改变 ID。样例数量不等于用于构图的数据量。

## 接口与权限

```
GET /api/personal-datasets/{dataset_id}/construction/source-preview
GET /api/projects/{project_id}/datasets/{dataset_id}/construction/source-preview
```

CSV 可加 `?delimiter=`，支持逗号、制表符、分号、竖线或空格。接口沿用数据集所有权、项目作用域和服务间鉴权；路径由可信的数据集记录决定，客户端不能提交服务器源文件路径。

响应包括 `dataset_id`、`source_type`、`tables`、可选 `text`、`warnings` 和 `limits`。每表包括 `columns`、`sample_rows`、`row_count`、`column_types`、`primary_key`、`foreign_keys` 和带 `source` 的 `column_descriptions`。`sample_truncated` 表示还有未展示的记录，`values_truncated` 表示样例中的长单元格被截断；两者分别标识。SQLite 大于样例数量时不扫描全表，`row_count` 为 `null`；文本 SQL 的行数只统计成功识别的 VALUES，样例之外的 VALUES 也会校验列数。

## 预览边界

SQL/CSV 输入及 CSV 包总解压大小上限 32 MiB，最多 128 张表、每表 256 列，响应上限 4 MiB UTF-8 字节。SQL 另设解析时间、每条语句和总 token 预算。SQLite 以只读方式打开，禁用可信 schema，并拒绝生成列及隐藏列，限制内部单值大小。超限会显示可重试的预览错误，不会把不完整预览伪装成完整数据。

这些是预览接口的限制，与构图算法的限制分别执行。预览失败不自动阻止构图，构图成功也不代表所有 SQL 方言都可预览。
