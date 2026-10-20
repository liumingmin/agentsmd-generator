# agentsmd-generator

为代码仓库生成带源码行号的项目导览文档，让 AI 编程助手和开发者更快理解项目结构、模块职责与关键实现。

本项目是一套由 [`SKILL.md`](SKILL.md)、静态分析脚本和文档模板组成的 Skill，生成两层文档：

- **`AGENTS.md`**：项目总入口，包含项目定位、模块地图、架构与数据流、构建运行方式、代码统计和开发约定。
- **`AGENTS.<模块名>.md`**：模块说明，包含功能点、核心类型、对外接口、依赖协作以及对应的源码位置。

> 脚本负责提取代码索引、生成辅助报告和校验引用，不会单独生成完整的 Markdown 导览文档。文档需要由 AI 编程助手按照 `SKILL.md` 阅读源码、归纳并编写。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| Go AST 分析 | 提取包、文件、函数、方法、类型、常量、变量的起止行号，以及注释、参数和返回值摘要 |
| 字段级索引 | 提取 Go 结构体字段、tag、嵌入字段和接口方法 |
| 多语言索引 | 对非 Go 代码进行正则扫描，辅助识别类、函数、接口、表定义等符号 |
| 路由与任务线索 | 从 Go 代码中识别特定形式的 HTTP 路由注册，以及字符串和注释中的 cron 表达式 |
| 维护热点 | 根据文件行数、函数跨度和结构体字段数，输出规模热点报告 |
| 模块依赖 | 基于 Go imports 聚合模块依赖关系和被依赖排行 |
| 增量更新 | 对比新旧索引，报告符号、字段、路由和 cron 变化，并定位受影响的文档引用 |
| 文档校验 | 检查源码引用、字段行号、相对链接及部分统计信息 |

Go 和 Python 工具均仅使用标准库，无需安装第三方依赖，也没有内置模型 API 调用。

## 环境要求

- **Python 3**：运行 `scripts/*.py`。下文统一使用 `python3`；Windows 可根据环境改为 `python` 或 `py -3`。
- **Go 1.24.3 或更高版本**：编译或运行 Go AST 分析器，版本要求以 [`scripts/goast/go.mod`](scripts/goast/go.mod) 为准。仅分析非 Go 项目时不需要 Go。
- **AI 编程助手**：用于执行 Skill 工作流、阅读源码和编写文档；也可以人工完成这部分工作。

## 快速开始

### 1. 获取项目

```bash
git clone https://github.com/liumingmin/agentsmd-generator.git
cd agentsmd-generator
```

需要分析 Go 代码时，先编译分析器：

```bash
cd scripts/goast
go build -o goast .
cd ../..
```

Windows 下可使用 `go build -o goast.exe .`。仓库不包含预编译二进制，需要自行构建。

### 2. 让 AI 助手执行 Skill

在支持 Skill 的助手中加载本项目的 `SKILL.md`，或直接让助手读取它，并提供待分析项目的绝对路径。例如：

```text
请读取 /path/to/agentsmd-generator/SKILL.md，
按照其中的流程，为 /path/to/your-project 生成 AGENTS.md
和各模块的 AGENTS.<模块名>.md。
结合源码归纳功能，标注关键代码位置，生成后执行校验。
```

Skill 中定义的常见触发词包括：

- 生成 AGENTS.md
- 分析项目生成模块文档
- 生成模块说明并带行号
- 给项目写导航文档
- 更新 AGENTS.md

完整流程为：**静态分析 → 补充项目信息 → 划分模块 → 编写模块文档 → 编写根文档 → 校验与人工复核**。

### 3. 查看生成结果

默认将导览文档放在目标项目根目录：

```text
your-project/
├── AGENTS.md
├── AGENTS.api.md
├── AGENTS.storage.md
├── .agentsmd/
│   ├── ast.json
│   └── index.json
└── ...
```

模块名由项目实际结构决定；纯 Go 或纯非 Go 项目只需要对应的索引文件。`.agentsmd/` 保存中间分析结果，建议保留以便后续增量更新。

## 单独使用分析工具

以下示例使用 Bash / Zsh，在本仓库根目录执行。请将 `PROJECT_ROOT` 替换为待分析项目的绝对路径。

```bash
SKILL_DIR="$(pwd)"
PROJECT_ROOT="/absolute/path/to/your-project"
```

### Go 代码索引

```bash
"$SKILL_DIR/scripts/goast/goast" \
  -root "$PROJECT_ROOT" \
  -out "$PROJECT_ROOT/.agentsmd/ast.json"
```

主要参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-root` | `.` | 待分析项目根目录 |
| `-out` | 不指定 | JSON 输出路径；不指定时写入标准输出 |
| `-test` | `true` | 是否包含 `_test.go` 文件；使用 `-test=false` 排除 |

输出包含 `root`、`packages`、`files`、`stats`，识别到相关内容时还包含 `routes` 和 `crons`。分析器会跳过隐藏目录，以及 `vendor`、`node_modules` 等目录；解析失败的文件会输出警告并跳过。

### 非 Go 代码索引

```bash
python3 "$SKILL_DIR/scripts/codeindex.py" \
  --root "$PROJECT_ROOT" \
  --out "$PROJECT_ROOT/.agentsmd/index.json"
```

可通过 `--ext` 限定文件扩展名：

```bash
python3 "$SKILL_DIR/scripts/codeindex.py" \
  --root "$PROJECT_ROOT" \
  --ext .ts,.tsx,.py \
  --out "$PROJECT_ROOT/.agentsmd/index.json"
```

支持 TypeScript、JavaScript、Python、Java、Kotlin、Rust、C/C++、SQL、Protocol Buffers 和 Shell。不同语言仅识别预设规则覆盖的符号，不提供完整语法或语义分析。

混合语言项目应分别生成 `ast.json` 和 `index.json`，结合两份结果编写文档。

### 维护热点与模块依赖

```bash
python3 "$SKILL_DIR/scripts/hotspots.py" \
  --ast "$PROJECT_ROOT/.agentsmd/ast.json"

python3 "$SKILL_DIR/scripts/callgraph.py" \
  --ast "$PROJECT_ROOT/.agentsmd/ast.json" \
  --root "$PROJECT_ROOT"
```

`hotspots.py` 默认报告超过 500 行的文件、跨度超过 100 行的函数，以及超过 20 个字段的结构体；可用 `--file`、`--func`、`--struct` 和 `--top` 调整。

`callgraph.py` 从目标项目根目录的 `go.mod` 自动探测 module 前缀，也支持 `--module example.com/your-project` 显式指定。输出是 **import 级模块依赖图，不是函数调用图**。

### 校验导览文档

编写好 `AGENTS*.md` 后执行：

```bash
python3 "$SKILL_DIR/scripts/verify.py" --root "$PROJECT_ROOT"
```

统计不一致也需要判为失败时，加上 `--strict`：

```bash
python3 "$SKILL_DIR/scripts/verify.py" --root "$PROJECT_ROOT" --strict
```

默认读取目标项目的 `.agentsmd/ast.json`。非 Go 项目可通过 `--ast` 指定轻量索引：

```bash
python3 "$SKILL_DIR/scripts/verify.py" \
  --root "$PROJECT_ROOT" \
  --ast "$PROJECT_ROOT/.agentsmd/index.json"
```

校验器当前仅扫描项目根目录的 `AGENTS*.md` / `agents*.md`，相对链接也从项目根目录解析，建议将生成文档统一放在根目录。退出码为 `0` 表示无错误，`1` 表示校验失败；警告仍需人工确认。

## 增量更新

以下为 Go 项目的更新流程，沿用前文的 `SKILL_DIR` 和 `PROJECT_ROOT`：

```bash
# 备份上一次分析结果
cp "$PROJECT_ROOT/.agentsmd/ast.json" "$PROJECT_ROOT/.agentsmd/ast.old.json"

# 针对最新代码重新生成索引
"$SKILL_DIR/scripts/goast/goast" \
  -root "$PROJECT_ROOT" \
  -out "$PROJECT_ROOT/.agentsmd/ast.json"

# 输出变化报告及受影响文档的引用位置
python3 "$SKILL_DIR/scripts/diff.py" \
  --old "$PROJECT_ROOT/.agentsmd/ast.old.json" \
  --new "$PROJECT_ROOT/.agentsmd/ast.json" \
  --root "$PROJECT_ROOT"
```

让 AI 助手结合报告复核并更新文档，然后重新执行 `verify.py`。确认完成后可删除备份文件。`diff.py` 只输出报告，不会自动修改文档；显示“无实质变化”也仅表示其跟踪的索引信息未变化，不代表函数内部行为完全未变。

## 目录结构

```text
agentsmd-generator/
├── README.md
├── SKILL.md                         # AI 助手执行流程与生成约定
├── LICENSE                          # MIT 许可证
├── references/
│   └── module-doc-template.md       # 模块文档模板与行号引用规则
└── scripts/
    ├── goast/
    │   ├── go.mod
    │   └── main.go                  # Go AST 分析器
    ├── codeindex.py                 # 多语言正则索引器
    ├── hotspots.py                  # 规模热点报告
    ├── callgraph.py                 # import 级模块依赖报告
    ├── diff.py                      # 索引差异与文档影响报告
    └── verify.py                    # 导览文档校验器
```

## 使用边界

- **源码优先**：导览文档是导航辅助，代码变更后应重新分析并更新引用。工作流只新增或更新导览文档及分析产物，不修改业务源码。
- **行号与签名**：Go 声明位置来自 AST；签名是简化摘要，并非完整类型检查结果。非 Go 索引使用正则，结束行与导出性等字段不能按精确语义解读，引用前需打开源码核实。
- **路由与 cron**：按语法模式识别，不会完整还原动态路由、路由组前缀、运行时调度或所有框架写法，需要人工复核。
- **校验不等于语义审查**：自动校验通过不代表功能描述正确，也不代表所有语言、链接和行号格式均被覆盖。
- **多语言辅助能力有限**：非 Go 索引不提取 imports，不能据此生成完整依赖图；热点分析也无法准确计算非 Go 函数跨度。混合项目需分别核对索引与统计，校验器不会自动合并两份报告。

## 许可证

本项目使用 [MIT License](LICENSE)。
