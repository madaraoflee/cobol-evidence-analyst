# Public code naming rule

源码、代码配置、代码注释和测试夹具不得出现任何能够识别内部机构、客户、团队、系统或供应商的专有标识。统一使用中性的业务名称；这条规则只针对代码相关文件，不要求改写项目文档或历史记录。

# Desktop release acceptance

正式 Windows 发布须使用 `tools/publish_desktop_release.py`，执行 `docs/joint-release-acceptance.md` 的同包联合验收。不得直接修改草稿状态绕过门禁；源码测试、合成模型、未评分表单或缺失的目标设备验收不能代替最终 EXE 的真实验收。缺项时保留候选，已发布版本不自动替换。真实模型调用与源码外传仍须在既有授权范围内。

# Current bug-fix phase

自 2026-10-19 起，以修复 bug、源码测试及提交 GitHub 为主。用户再次明确允许生成前，不得构建可运行 app、安装包、验收工具可执行文件或 release，也不得触发相关 GitHub Actions。桌面与办公室验收工作流仅保留手动入口，不因推送、拉取请求或其他工作流完成而自动构建。恢复生成仍须遵守上述发布验收要求。
