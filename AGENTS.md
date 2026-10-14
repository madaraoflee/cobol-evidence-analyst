# Public code naming rule

源码、代码配置、代码注释和测试夹具不得出现任何能够识别内部机构、客户、团队、系统或供应商的专有标识。统一使用中性的业务名称；这条规则只针对代码相关文件，不要求改写项目文档或历史记录。

# Desktop release acceptance

正式 Windows 发布须使用 `tools/publish_desktop_release.py`，执行 `docs/joint-release-acceptance.md` 的同包联合验收。不得直接修改草稿状态绕过门禁；源码测试、合成模型、未评分表单或缺失的目标设备验收不能代替最终 EXE 的真实验收。缺项时保留候选，已发布版本不自动替换。真实模型调用与源码外传仍须在既有授权范围内。
