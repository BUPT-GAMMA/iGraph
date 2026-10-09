# Finance 13 案例系统验算报告

日期：2026-09-27。案例来源：`F:/mypaper/data/finance/cases.py`；参考实现为同目录 `citics/citics/faction_engine`，版本 0.5.0。原参考目录未修改。

## 结论

- 修复后，13 个案例的 65 次实际系统 BFF 调用全部返回有效业务结果，13/13 通过数值、排名、结构和质量状态对照。
- 12 个可收敛案例经济权益与参考一致；100% 互持案例正确标记不收敛。根数值最大绝对误差为 5.87648e-13，验收容差为 1e-10。
- 原系统 10 个纯机构案例被个人表缺失错误阻断。修复后无需虚构个人节点；真正缺失的个人引用仍然报错。
- 7 个压力案例的持股合计超过 100%，系统保留 DATA_CONFLICT。这与经济数值算对可以同时成立，不能为了匹配参考而取消质量校验。
- **自然语言验收没有全部通过。** 当前默认模型在最后一次不收敛案例复测中，仍将控制候选 A 自行写成首大候选，虽然工具明确返回 `first_holder_ids=[]`、`first_holder_id=null`。这是尚未解决的解释错误，不能用接口通过率掩盖。

## 测试方式

1. 执行参考包计算，并用 Fraction 全局有理数高斯消元独立复核 `(I-A)s=e_target`。参考包自身 44 个单元测试通过。
2. 在实际前端对应服务创建独立项目“Finance cases 回归验证 2026-09-27”，逐例通过公开上传接口导入原始记录并激活会话。13 个数据集的节点类型、方向和所有比例与原输入逐项一致。
3. 每个会话运行 inspect、first-holder、control-chain、economic-interest、group-tree，共 65 次真实 BFF 调用。记录部署前与部署后原始响应；未以本地函数运行冒充线上验证。
4. 核对根排名、累计权益、目标与 SCC 成员权益、SCC 划分、非环控制根/中间控制人、首大候选、投影完整性和质量阻断。另对 131 个节点执行指定上游权益回归。
5. 在部署镜像对应的隔离验证环境执行金融算法、HTTP、MCP、工作流契约回归：158 passed、12 skipped。跳过项是未启用的可选/外部数据测试。

## 逐案例结果

“首大”均指工具候选；经济数值按份额小数展示。DATA_CONFLICT 下不能确认业务首大或控制关系。表中控制状态为算法诊断，质量冲突另列。

| 案例 | 首大候选 | 控制算法诊断 | 质量冲突 | 主要经济根累计值 |
|---|---|---|---|---|
| pyramid | B | CONFIRMED_CONTROL，根 A、中间 B | 无 | A=0.36 |
| same-ratio | A | NO_SINGLE_CONTROLLER | 无 | A=0.40，保留两条等权路径 |
| cumulative-root | A | CONFIRMED_CONTROL，根 A | 无 | A=0.64，D=0.25 |
| cross-holding | B，SCC_REVIEW | SCC_REVIEW | 无 | B=1.09375，A=0.65625 |
| non-convergent-cycle | 无 | SCC_REVIEW | 无 | NON_CONVERGENT |
| fifty-fifty | A、B 并列 | NO_SINGLE_CONTROLLER | 无 | A=0.50，B=0.50 |
| multi-scc-chain | B，SCC_REVIEW | NO_SINGLE_CONTROLLER | T=126% | B≈0.317460822111，A≈0.190533545287 |
| scc-expansion | E，SCC_REVIEW | SCC_REVIEW | F=102% | E≈0.631369869198 |
| dense-cross-network | A，SCC_REVIEW | NO_SINGLE_CONTROLLER | TARGET=102% | A≈0.154980498345 |
| person-aligned-network | P1 | NO_SINGLE_CONTROLLER | TARGET=137% | P1≈0.070790076448，P2≈0.062756851416 |
| parallel-root-contest | A | MULTIPLE_CONTROLLERS，A/B/C | TARGET=335% | A=1.08，B=0.87，C=0.611 |
| mega-network | P3 | NO_SINGLE_CONTROLLER | TARGET=235% | P3≈0.060806782451，P2≈0.055124426598，P1≈0.024090219866 |
| multi-scc-economic-target | Q | NO_SINGLE_CONTROLLER | B1=107%，TARGET=112% | Q≈0.113370755270，S=0.0777，R≈0.076199387924，P≈0.067702095620 |

## 参考文件自身的差异

`expected` 是历史断言，参考程序实际只满足其中 9/13。以下 4 个案例共 5 个字段与配套算法不同：

| 案例 | 字段 | cases.expected | 参考程序实际 |
|---|---|---|---|
| multi-scc-chain | holder_status | SCC_REVIEW | COMPLETE |
| dense-cross-network | control_status | SCC_REVIEW | NO_SINGLE_CONTROLLER |
| person-aligned-network | holder_status | SCC_REVIEW | COMPLETE |
| person-aligned-network | rule | MAX_CUMULATIVE_ROOT | MAX_ECONOMIC_INTEREST_SCC |
| mega-network | holder_status | SCC_REVIEW | COMPLETE |

系统保留以下有意的策略差异：

- 直接股东/经济根处于 SCC 时，系统保留 SCC_REVIEW；参考可能在经济方程收敛后将首大标 COMPLETE。数值收敛并不能确认控制，未强行消除该差异。
- 50/50 时系统用 TIED_CANDIDATES 明示首大并列；参考返回 COMPLETE，但双方都保留 A、B 并列，且不确认单一控制人。
- 多控制候选系统名称为 MULTIPLE_CONTROLLERS，参考为 TIED_CANDIDATES，候选集合一致。
- 互持案例中参考可能给出待复核 ultimate 候选；系统保留全部 controller_ids，并在无法确定唯一最终根时留空 ultimate。双方均要求 SCC_REVIEW，不能把候选当已确认根。
- 参考没有系统的股份总和质检，因此系统的 7 例 DATA_CONFLICT 是附加质量门槛，不是权益数值偏差。

## 已更新内容

- 金融投影允许纯机构图没有个人表记录，保留多态引用检查。
- 直接过半首大分支检查相关 SCC 的经济求解；100% 互持不再漏掉 NON_CONVERGENT，也不会清除不受异常少数股东影响的直接多数候选。
- 超出 SCC 求解上限明确返回 TRUNCATED，不把“未计算”说成“不收敛”。
- workflow 区分历史 expected、参考实算、系统质量状态；明确 target_interest 的单位注入含义、覆盖缺口计数、规则缺失下的候选称呼，以及完整累计和 Top-K 展示的区别。
- workflow 明确并列依据累计候选相等，不以“无人直接过半”推断首大并列；不建议删环或虚构数据制造经济收敛。
- 新增 13 案例固定输入、参考/独立数值、可复用验算脚本与 40 项回归测试。

## 复现与证据

本地无需外部参考目录即可运行：

```text
python scripts/validate_finance_cases.py --prefix local
```

实际测试项目：[Finance cases 回归验证](http://127.0.0.1:21108/?project=a7666441-76b4-4fed-a92a-5cfe34e2dd6e)。

详细输出位于 `output/finance-case-validation/`：

- `live_before_full.json` / `live_after_full.json`：实际接口前后完整响应。
- `live_before_comparison.json` / `live_after_comparison.json`：逐项断言、状态差异与误差。
- `reference_ground_truth.json` / `reference_full_unfiltered.json`：参考实际结果及无阈值复核。
- `independent_economic_verification.json`：独立有理数求解。
- `live_session_map.json` / `import_fidelity.json`：真实会话与无损导入核对。
- `pytest.xml` / `deployment-health.txt`：容器测试和部署证据。

## 真实聊天验收

用系统默认模型 `qwen3.6-plus` 对 13 个独立会话逐例发送同一分析模板，仅提供目标 ID，不提供 ground truth。13/13 任务完成，全部实际读取金融 workflow，并调用检查、首大、控制链和经济权益工具；聊天里的工具返回与直接 BFF 验算一致。

初次生成的自然语言仍发现若干解释错误，不能将“工具正确”当作“整篇回答完全正确”：单位注入误称股权总额、把覆盖缺口说成完全没有股东、用根值总和推算剩余权益、建议过滤环路制造收敛、预先承诺数据冲突修正后即可确认，以及把全图截断事实当作完整目标股东。已逐条更新 workflow，保留原始回复及 `chat_semantic_findings.json`，并以全新会话对 7 个代表案例定向复测。

13 个初次任务、7 个定向新会话和最后 1 个不收敛案例新会话均已完成。全部 21 次聊天的金融工具输出与对应 BFF 数值和状态一致；实际读取的 workflow 正文有独立哈希证据。最终 65 次接口复测仍为 13/13 通过。

定向复测已观察到改进：单位注入含义得到纠正，多 SCC 案例不再编造 49.2% 的余量归属，不再建议 stop/exclude 制造经济收敛，不再承诺修正 102% 后即可确认，mega 案例直接合计正确引用 235%，没有把展示小计 115% 当全量。

仍保留以下未解决项：

- **不收敛案例首大候选被自补。** 最后一次确已加载新增的“空数组必须写未返回候选”规则，正文前面也写了 null，结尾却仍写“A 为股权口径首大候选”。正确结论是：首大工具未返回候选；A 是原始直接持股主体和控制工具中的待复核候选。见[最终复核会话](http://127.0.0.1:21108/?session=4d5f8fd1-8192-4774-abe2-a3f112f27ce2)。单靠本次 workflow 提示词更新未消除该默认模型行为。
- cumulative-root 仍将根 64%+25% 后的 11% 归给未记录股东。本例直接持股合计恰好也是 89%，但这种从根累计值推算余量的表述不能推广到一般图。
- fifty-fifty 仍将 confirmed_id 为空的原因简化成“没有直接股东过半”；实际应同时解释累计候选并列和业务规则缺失。
- 7 次定向复测中只有 4 次按 query 检查，另外 3 次仍先获取全图有界快照；本批核心数值正确，但 workflow 的检索步骤未被模型严格遵循。

因此，本报告只对确定性计算给出通过结论，不宣称自由文本解释全部正确。用户核验首大和控制候选时，应分别读取相应工具字段，不能跨分支补候选。

初次聊天的完整 SSE、最终回复和运行快照保存在 `chat_*.json`，工具审计为 `live_chat_audit.json`。最终接口复测为 `live_final_*.json`；定向聊天证据为 `recheck_*.json`。逐项语义结果为 `recheck_semantic_audit.json`，最后候选错误的证据为 `final_nonconv_semantic_audit.json`。最后一次 workflow 更新后的工具契约测试另有 8 项通过。

## 部署

实际访问地址 `127.0.0.1:21108` 是已有 SSH 隧道转发，运行项目为 `igraph-bank`。已在该项目重建并启动 `graph-engine-api`、共享图镜像的 `graph-engine-worker` 和 `agent-runtime`。API/Runtime 及其他有健康检查的服务均健康，worker 正常运行，前端 `/health` 返回 ok。

最终容器内 `finance.py` 与 workflow 的 SHA256 分别为 `a1075c62de4b4a7f2bfea9ca39c74ae74c09aad4a5b28d87dd668b52c0817254`、`4fa2e1e64cf548daf3ef95ac43f3945c7ff4e6fcdebf6570c1932c15b37ff386`，与本地文件一致。仅同步本次金融实现、workflow、回归脚本/fixture/test 和本报告，未覆盖其他开发改动。
