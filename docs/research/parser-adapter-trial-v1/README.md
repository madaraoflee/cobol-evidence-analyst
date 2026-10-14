# 固定版本原生解析器实际试接

本轮已经实际编译并调用 ProLeap v2.4.0 的 `CobolParserRunnerImpl.analyzeFile`，得到 ASG 和语法树，再通过实验性 Python/JSON 桥转为项目自己的 IR。默认工作台继续使用 Python 前端；原生组件没有成为启动依赖，Java 对象不会进入业务层。真实结果和两份规范化 IR 位于 [results.json](results.json)。

先核对 [官方 v2.4.0 release](https://github.com/uwol/proleap-cobol-parser/releases/tag/v2.4.0) 和 [固定 tag 的构建定义](https://github.com/uwol/proleap-cobol-parser/blob/v2.4.0/pom.xml)。该版本的产物名称为 `io.github.uwol:cobol85parser:2.4.0`；本机实际检查 Maven Central 固定路径返回 404，README 所列专用 Maven 库 HTTPS 握手未成功。因此使用固定 tag 原源码在 `/private/tmp/native-parser-trial` 隔离构建，没有假定存在可用预编译 JAR。

固定源码 tar.gz 的 SHA-256 为 `477e31e5984f307545a0ae3e5bc8351d40a3d128c9f471de7dfe4eb63ef96051`。采用 ANTLR 4.7 和 ECJ 3.33.0 生成、编译 1,129 个 Java 源文件；成功编译耗时 2.391 秒。使用 [官方 Temurin 17.0.16+8](https://github.com/adoptium/temurin17-binaries/releases/tag/jdk-17.0.16%2B8) 便携 macOS arm64 JRE，下载包 SHA-256 `30c3cfb717a2a84bf164c010a9c51dc81a15dcf214cf83a9695206233c9c5d37` 与官方校验文件一致。所有依赖版本与实际哈希都记录在结果中；源码、JRE、依赖、生成类和原许可留在临时目录，没有复制或安装到产品环境。

6 个原创极小探针覆盖 ADD TO GIVING 的不同/相同输入输出字段、结束词、同物理行多个 MOVE、限定字段，以及本桥尚未转换的 IF 作用域。比较结果为 **12 个规范化语句正确、1 个不支持、0 个错误**；计数包含简单 GOBACK，不能解读为 12 道业务题或完整语言覆盖率。不支持的 IF 写入保留诊断，未为了相同输出删除诊断或冒充已建立流关系。

统一前端最终升至 `cobol-facts-v5` 后，已真实重新执行以上原生探针；结果保留实际编译上下文/指纹、Builtin adapter 版本、每个完整合成源码哈希及两份 IR，没有只改历史报告的标签。

原生桥分别从原生 COMPUTE、MOVE 和 ADD GIVING 语法节点提取操作数角色和表达式。字段声明、符号绑定和保守布局仍使用共同的本地字段服务；本轮没有把这部分称为原生 ASG 字段/存储/数据流对比，也没有验证 COPY、复杂条件、企业方言、异常、SQL/CICS、循环或跨程序摘要。ProLeap 此固定版本不提供 FREE 格式枚举，实验对无续行源码每行添加一个中性 TANDEM 指示列，再明确减去该列恢复原始位置；复杂预处理位置映射不在本轮支持范围。

已有隔离构建目录时，可重复执行实际桥测试：

```bash
python3 docs/research/parser-adapter-trial-v1/run_trial.py --runtime-root /private/tmp/native-parser-trial --output /path/to/native-results.json
```

构建路径仅执行固定 ANTLR 生成器、固定 ECJ 编译器和本项目 [SyntaxProbe.java](SyntaxProbe.java)，没有运行第三方项目的安装或网络脚本。重建时需从固定 tag 获取源码，确认以上 archive hash；生成两个 `src/main/antlr4/io/proleap/cobol` grammar，指定 Java package `io.proleap.cobol`；用明确 JAR 列表作为 classpath 编译生成源码、`src/main/java` 和本探针。JRE 及各依赖先完成来源/哈希核对。本桥 [bridge.py](bridge.py) 使用进程上限和固定文件哈希；版本或映射不一致输出诊断。

Che4z 2.5.1 作为复杂预处理、方言和位置映射的独立候选，依据 [官方 release](https://github.com/eclipse-che4z/che-che4z-lsp-for-cobol/releases/tag/2.5.1) 固定版本。本轮没有构建或执行它，不借用 ProLeap 结果证明其覆盖率，也不把尚未配置的候选当作已可用解析器。

本地算术 IR 保持十进制字面量原文并使用 COBOL 的运算符优先级及同级从左到右顺序；这来自 [官方算术运算符定义](https://www.ibm.com/docs/en/cobol-zos/6.3.0?topic=expressions-arithmetic-operators)。它是表达式结构，不是完整 COBOL 算术执行器。COPY 的嵌套 REPLACING 单链限制与 [官方 COPY 定义](https://www.ibm.com/docs/en/cobol-zos/6.4.0?topic=statements-copy-statement) 对照；是否允许嵌套替换仍由明确目标 profile 决定，未知配置保留边界。
