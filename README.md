# agentsmd-generator

分析源码、测试与配置，帮助 AI 编程助手编写可回溯证据的项目导览：根 `AGENTS.md` 提供项目地图，分层 `AGENTS.<模块>.md` 解释具体职责、行为、接口和验证方式。

脚本提取索引、生成模块候选、检查证据与增量影响；完整说明仍需助手阅读源码后归纳。自动校验不等于功能语义正确或测试覆盖充分。

## 能力

| 能力 | 实际行为 |
| --- | --- |
| 统一扫描 | 所有索引共享文件清单、Git ignore/显式排除策略和范围哈希 |
| 语法索引 | Go/Python AST；JS/TS 可选 TypeScript 编译器 AST，缺失时明确降级 |
| 分类统计 | production/test/generated 分开计数、分别报告热点 |
| 项目资产 | 配置、CI、嵌套 manifest、迁移、测试、文件哈希和部分清单事实 |
| 阅读新鲜度 | 阅读记录绑定全部归属源码哈希；代码变化后完整审阅过期，显式复核后记录 |
| 模块覆盖 | 源码唯一归属、未覆盖/重复归属检查、父子导航、独立阅读与测试记录 |
| 分层规划 | 超预算目录递归拆分；同目录大模块提示按实际职责人工划分 |
| 结构化证据 | Markdown 结论绑定实现、调用、配置和测试；校验文案及片段/符号指纹 |
| 增量影响 | 符号体/契约/位置变化、新旧 import 图反向传递、受影响模块和结论 |
| 文档框架 | 生成可编辑导航、分类统计、行号引用及明确待核实的证据草稿；保护人工区块并限制文档写入目标 |
| 一致快照 | 不可变版本目录与原子清单切换，整组读取、校验哈希并保留历史版本 |
| 文档检查 | 当前源码行号、链接目标、范围与索引新鲜度、统计和证据检查 |

## 环境

- Python 3.10+，Python 工具仅依赖标准库。
- 含 Go 时需要 Go 1.24.3+（以 `scripts/goast/go.mod` 为准），不构建目标项目。
- JS/TS AST 可选 Node 和本地 TypeScript 编译器；不自动联网安装依赖，不执行目标程序。

## 使用 Skill

让助手读取 [`SKILL.md`](SKILL.md)，并提供目标项目绝对路径：

```text
请按照 /path/to/agentsmd-generator/SKILL.md，
为 /path/to/project 生成项目导览与模块说明。
阅读实际实现，登记模块归属和主要结论证据，完成校验。
```

工作流为：统一扫描 → 审阅模块边界 → 阅读关键实现 → 编写分层文档与证据 → 校验与人工复核。保留目标仓库已有的人工指令，仅更新授权的导览和分析产物。

## 运行工具

以下命令中的路径需替换为实际绝对路径。

```bash
# 自动构建 Go 分析器到临时目录；无 Go 项目不需要 Go
python3 /path/to/skill/scripts/analyze.py --root /path/to/project

# JS/TS 必须使用 AST 时，显式提供编译器；auto 模式缺失时报告降级
python3 /path/to/skill/scripts/analyze.py --root /path/to/project \
  --js-parser typescript --typescript /path/to/typescript/lib/typescript.js
```

输出 `.agentsmd/selection.json`、`context.json`、`index.json`、可选 `ast.json` 和 `module-candidates.json`。先查看 `diagnostics` / `notices`，再审阅候选模块职责及文档名，将结果保存为 `modules.json`。工具不自动把阅读状态标成已审阅。报告发布到 `.agentsmd/snapshots/<版本>/`，由 `current.json` 选择整组；上述逻辑路径由配套脚本解析。查看 JSON 用 `snapshots.py show --dir /path/to/project/.agentsmd --artifact index.json`，不直接解析逻辑引用文件。

默认超过 64 个非生成文件或 500 个非生成符号时尝试递归拆分，使用 `--max-files` / `--max-symbols` 调整。父文档负责整体说明和导航，子文档负责实现细节，源文件不重复归属。未支持语法的源码仍需分配模块。

```bash
# 写作前只检查归属
python3 /path/to/skill/scripts/modules.py check --root /path/to/project --no-docs

# 生成预览：导航、实际统计、候选行号和明确待核实的证据草稿
python3 /path/to/skill/scripts/draft.py --root /path/to/project
# 已有预览时默认继续，保留其基线与审阅记录；新会话必须换用空目录
# python3 /path/to/skill/scripts/draft.py --root /path/to/project --new-preview --out-dir .agentsmd/drafts/next-review

# 实际复核后记录预览中指定模块的源码版本，不自动提升阅读范围
python3 /path/to/skill/scripts/modules.py record-review --root /path/to/project \
  --map /path/to/project/.agentsmd/drafts/modules.draft.json --module storage

# 确需执行测试时，记录实际结果及执行时的输入版本；不隐式运行任何命令
python3 /path/to/skill/scripts/teststate.py --root /path/to/project \
  --map /path/to/project/.agentsmd/drafts/modules.draft.json --module storage --cwd . -- python3 -m unittest discover -s tests

# 阅读记录和预览正文就绪后，用配套预览地图/ledger 捕获证据
python3 /path/to/skill/scripts/evidence.py capture --root /path/to/project \
  --map /path/to/project/.agentsmd/drafts/modules.draft.json \
  --ledger /path/to/project/.agentsmd/drafts/evidence.draft.json

# 先用预览地图和证据运行对应交付模式的 verify，再转正到最终文档及正式 map/ledger
python3 /path/to/skill/scripts/draft.py --root /path/to/project --promote

# 交付检查：包括阅读记录、测试处置与已确认的行为证据
python3 /path/to/skill/scripts/verify.py --root /path/to/project --require-reviewed
```

模块、证据、扫描配置、状态和统计格式见 [`references/analysis-artifacts.md`](references/analysis-artifacts.md)，写作粒度见 [`references/module-doc-template.md`](references/module-doc-template.md)。`--require-reviewed` 适合完整交付；分批审阅时使用 `--delivery sampled` 并在文档中明确抽样范围。完整交付要求所有文件有阅读/排除处置，且主要能力的输入、成功、失败与配置方面有匹配结论或不适用理由；旧地图需要补齐字段。`--strict` 让警告也视为失败。完整交付会拦截生成占位章节和草稿标记；有理由的实际未知事项仍可单独记录。阅读哈希过期需要先复核代码，再 record-review，不能通过重跑 capture 自动刷新。

校验默认按模块和类别聚合，`--details` 展开全部诊断，`--json` 输出完整结果与精确字段位置；字段结构有误时停止处理，不输出异常堆栈。

完整检查还要求必要说明有正文，不能仅保留导航或空标题；支持中英文标题及 `agentsmd:section` 自定义章节标记。测试结果绑定扫描范围内源码、测试、配置与依赖清单，输入变化后只作为历史结果，重新记录阅读不会刷新测试。生成器按方面寻找候选证据，缺失时明确标注。批量生成中断后，执行 `draft.py --root /path/to/project --recover` 恢复；冲突编辑会保留并报告，恢复前停止生成和交付。

扫描报告将命令行选项与实际合并策略分别保存，校验会重新读取当前项目配置；删除排除项也能发现新增应分析文件。旧报告缺少选项来源时需重新分析并核对地图。候选定位排除注释和字符串内容。预览记录最终路径及目标基线，--promote 保留人工正文、同步迁移导航和证据；--write 仅用于最终地图。转正不会确认草稿或刷新源码证明，目标出现其他编辑时会报告冲突。

默认重跑会继续已保存的预览会话，避免用新目标基线搭配旧正文；--new-preview 只在空目录创建。链接迁移保留标签、标题和代码示例，并支持引用式链接。JS/TS 候选依赖有效 TypeScript AST，整个 JSX 表达式保守跳过；缺少可靠解析时不给自动候选。

有模块地图时，校验其声明的交付文档，保留其他模板/指令文件的独立用途；没有地图时兼容递归检查 `AGENTS*.md`。标签路径相对项目根，Markdown 目标相对所在文档，例如 `docs/` 内的 `[src/a.go:L10-L20](../src/a.go#L10-L20)`。

## 单独分析与辅助报告

```bash
python3 /path/to/skill/scripts/scanpolicy.py --root /path/to/project > /path/to/selection.json
python3 /path/to/skill/scripts/projectcontext.py --root /path/to/project \
  --selection /path/to/selection.json --out /path/to/context.json
python3 /path/to/skill/scripts/codeindex.py --root /path/to/project \
  --selection /path/to/selection.json --out /path/to/index.json

# Go 分析器构建；Windows 二进制名使用 goast.exe
go build -C /path/to/skill/scripts/goast -o /path/to/goast .
/path/to/goast -root /path/to/project -selection /path/to/selection.json -out /path/to/ast.json

python3 /path/to/skill/scripts/hotspots.py --ast /path/to/ast.json
python3 /path/to/skill/scripts/callgraph.py --ast /path/to/ast.json --root /path/to/project
```

Python 索引工具支持 `--exclude`、`--no-gitignore`，`codeindex.py` 还支持 `--ext .ts,.py`。Go 单独运行未指定 `-selection` 时保留 legacy walk，扫描口径可能不同，推荐统一入口。扫描策略可配置在目标 `.agentsmd/config.json`；非 Git 副本不宣称应用 `.gitignore`。

## 增量更新

重跑统一入口保留不可变历史版本，`*.old.json` 指向上一整组；人工地图与证据清单不被覆盖。分别比较 `ast`、`index` 和 `context` 快照：

```bash
python3 /path/to/skill/scripts/diff.py \
  --old /path/to/project/.agentsmd/index.old.json \
  --new /path/to/project/.agentsmd/index.json --root /path/to/project --json
```

输出符号指纹变化、前后范围、保守文件级复核、传递导入影响、模块和证据结论；context 比较补充配置/CI/依赖变化。报告只提出复核范围，不自动改文字。阅读变更并确认结论后才重捕获证据，然后重新校验，避免用更新哈希掩盖过期说明。

## 能力边界

- AST 是静态语法分析，不执行类型检查或程序。Go 包含扫描到的构建变体；JS/TS 根据实际 tsconfig/jsconfig 解析别名及明确的对象导出；动态构造仍需人工确认。
- Java/Kotlin/Rust/C/C++/SQL/proto/shell 及降级 JS/TS 只有候选声明行，导出性未知，不计算完整函数规模。
- import 图不代表运行时调用图；跨语言桥接、动态依赖和未解析的外部 import 需人工追踪。
- 路由/cron 是候选，仍需核对真实注册、前缀、中间件、启用条件和时区。
- 文件归属覆盖、实现阅读与执行测试分别记录；工具不运行目标测试，不证明语义或覆盖率。只有显式登记的结论参与证据绑定检查。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
(cd scripts/goast && go test ./...)

# 将真实编译器后端纳入 Python 回归；未设置时相应测试明确 skip
AGENTSMD_TYPESCRIPT=/path/to/typescript/lib/typescript.js \
  python3 -m unittest discover -s tests -v
```

回归覆盖统一范围、Git 忽略规则、AST/指纹、模块遗漏/重复/分层、文案和片段过期、证据角色、测试记录、符号范围过滤、传递依赖和历史兼容行为。样例在临时目录生成。

## 许可证

[MIT License](LICENSE)。
