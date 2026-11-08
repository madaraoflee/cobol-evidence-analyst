# Public code naming rule

源码、代码配置、代码注释和测试夹具不得出现任何能够识别内部机构、客户、团队、系统或供应商的专有标识。统一使用中性的业务名称；这条规则只针对代码相关文件，不要求改写项目文档或历史记录。

# Desktop release acceptance

正式 Windows 发布须使用 `tools/publish_desktop_release.py`，执行 `docs/joint-release-acceptance.md` 的同包联合验收。不得直接修改草稿状态绕过门禁；源码测试、合成模型、未评分表单或缺失的目标设备验收不能代替最终 EXE 的真实验收。缺项时保留候选，已发布版本不自动替换。真实模型调用与源码外传仍须在既有授权范围内。

# Current bug-fix phase

自 2026-10-19 起，以修复 bug、源码测试及提交 GitHub 为主。用户再次明确允许生成前，不得构建可运行 app、安装包、验收工具可执行文件或 release，也不得触发相关 GitHub Actions。桌面与办公室验收工作流仅保留手动入口，不因推送、拉取请求或其他工作流完成而自动构建。恢复生成仍须遵守上述发布验收要求。

# Defect ledger and mandatory regression

每次遇到 bug 必须追加到 `docs/bugs/registry.json`，记录稳定编号、症状、原因、修复/复现证据、准确的回归测试和验证边界。修复后必须运行 `tools/verify_bug_regressions.py`，逐条核对全部已记录缺陷并执行全部现有 Python 与浏览器测试；同时维护只能增加的 `docs/bugs/regression-baseline.json`。不允许删除旧条目、削弱旧测试、跳过或标记预期失败来宣称通过。缺失、失败、未验证、执行中源码变化均不能算全部通过；`--check-only` 只检查登记，不能作为回归结果。每次报告必须说明仍未验证的现场问题，源码测试不能替代最终 EXE、真实模型和目标设备验收。操作细则见 [缺陷登记与逐次回归](docs/bugs/README.md)。该要求不解除本地链路冻结、构建/发布及真实模型授权限制。
