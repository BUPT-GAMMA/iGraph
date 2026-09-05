# CI 质量标准与覆盖率政策

适用范围：当前 iGraph 的前后端日常开发、PR 审查与发布验证。

## 1. 强制检查与基线门槛

| 检查 | 当前规则 |
| --- | --- |
| Python 核心及 Graph ML 自动化测试 | 继续要求通过；保留现有测试发现范围 |
| 前端单元与组件测试 | 继续要求通过 |
| TypeScript 类型检查与前端生产构建 | 继续要求通过 |
| 现有 Playwright E2E 套件 | 继续要求通过 |
| Docker Compose 配置校验 | 继续要求通过 |
| 前端行覆盖率 | 至少 44% |
| 前端语句覆盖率 | 至少 44% |
| 前端函数覆盖率 | 至少 59% |
| 前端分支覆盖率 | 至少 68% |
| Python 行覆盖率 | 至少 51% |
| Python 分支覆盖率 | 至少 34% |
| Python 综合覆盖率 | 持续生成报告，统一通过上述行/分支底线判断 |

这些门槛恢复到 dev 分支基线，取消改造保持完整统计范围和阻断能力。测试失败、
类型错误、构建失败或 E2E 失败仍会使 CI 失败，并由 `Required checks`
阻止后续生产发布。

Python 的 `.coveragerc` 保留 `branch = True` 和完整源代码统计范围；
`fail_under = 0` 仅关闭 coverage.py 的综合百分比门槛。
CI 继续调用 `scripts/check_coverage.py`，分别强制执行 51% / 34% 底线。

前端保留完整统计范围、V8 coverage provider 和全部覆盖率报告。
`frontend/vite.config.ts` 是配置源；仓库跟踪的 `vite.config.js` 为其编译产物，
修改后运行 `pnpm build` 并检查二者同步。

## 2. 改善目标与审查规则

- 整体行覆盖率以 **60%** 为阶段改善目标，按版本安排测试建设。
- 新增重要业务逻辑以 **约 80%** 覆盖率为审查参考目标，初期不作为新增自动阻断项。
- 当前流水线统计整体覆盖率；新增代码覆盖率暂由 PR 审查评估。
  后续引入差异覆盖率报告时，先作为信息展示并验证统计口径。
- 缺陷修复应附带能够复现原问题的回归测试，并验证修复后的预期行为。
  暂时无法自动化的场景应在 PR 中说明原因、提供可重复的验收步骤及后续补测安排。
- 优先覆盖会话/Run 状态转换、断流与恢复、租户权限隔离、文件上传边界、
  训练提交/取消/结束状态，以及正常、失败、并发和重试路径。
- 测试应断言可观察的行为、输出和副作用；审查时检查未覆盖路径的实际风险。

覆盖率表示测试执行过的代码范围。测试通过与覆盖率达标需要结合代码审查、
有效断言和功能验收共同判断。前端当前覆盖率来自 Vitest 单元/组件测试，
Playwright E2E 另行执行，其覆盖率尚未合并进这份报告。

## 3. 门槛维护

1. 按版本里程碑复核，期间保持门槛固定。
2. 提高门槛前，确认多次运行持续达标，并为后续开发保留明显余量。
3. 统计范围、coverage provider、工具版本或模块加载方式变化时，先比较报告分母与结果，
   核实差异原因；维持前端函数和分支硬门槛。
4. 调整门槛应通过明确说明原因的 PR，同步更新本政策，保留审查记录。
5. 代码缺陷通过修复和回归测试处理；覆盖率政策变更单独说明影响和适用阶段。

## 4. 本地验证

前端，在 `frontend` 目录运行：

```bash
pnpm test:coverage
pnpm build
pnpm test:e2e
```

Python 使用项目声明的 Python 3.12 与 `requirements-ci.txt` 环境。
可在仓库根目录一次性运行与 CI 相同发现范围的测试并检查报告：

```bash
python -m pytest -q --cov --cov-report=xml:coverage.xml --cov-report=json:coverage.json
python scripts/check_coverage.py coverage.json --lines 51 --branches 34
```

CI 为便于定位问题，将 Python 核心和 Graph ML 测试拆成两个步骤，随后合并报告、
执行同一门槛检查。完整流程见 `.github/workflows/ci.yml`。

## 5. 制定依据

- [Google：Code Coverage Best Practices](https://testing.googleblog.com/2020/08/code-coverage-best-practices.html)：
  按业务风险和测试成本选择目标，60% / 75% / 90% 为参考档位。
- [Sonar：Understanding quality gates](https://docs.sonarsource.com/sonarqube-cloud/standards/managing-quality-gates/introduction-to-quality-gates)：
  默认 Sonar way 关注新增代码的 80% 覆盖率。
- [Codecov：Status Checks](https://docs.codecov.com/docs/commit-status)：
  支持整体与差异覆盖率、下降容差及信息展示模式。
- [Martin Fowler：Test Coverage](https://martinfowler.com/bliki/TestCoverage.html)：
  使用覆盖率发现测试空白，结合行为断言判断测试质量。
