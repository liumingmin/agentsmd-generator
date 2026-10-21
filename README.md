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
| 多语言索引 | Python 使用 AST 提取类/方法、签名与 imports，其他语言提供声明行候选和目录分组 |
| 项目证据清单 | 发现嵌套 manifest、配置、CI、迁移和测试；提取部分清单事实并保存内容哈希 |
| 路由与任务线索 | 从 Go 代码中识别特定形式的 HTTP 路由注册，以及字符串和注释中的 cron 表达式 |
| 维护热点 | 根据文件行数、函数跨度和结构体字段数，输出规模热点报告 |
| 模块依赖 | 基于 Go imports 聚合模块依赖关系和被依赖排行 |
| 增量更新 | 通过内容哈希发现同签名函数体及配置变化，同时报告符号、字段/tag、imports 和接口候选变化 |
| 文档校验 | 同时检查多份索引、源码行号范围、链接目标一致性、索引新鲜度与显式统计标记 |

Go 和 Python 工具均仅使用标准库，无需安装第三方依赖，也没有内置模型 API 调用。

## 环境要求

- **Python 3.10+**：运行 `scripts/*.py`。下文统一使用 `python3`；Windows 可根据环境改为 `python` 或 `py -3`。
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
│   ├── context.json
│   ├── ast.json
│   └── index.json
└── ...
```

模块名由项目实际结构决定；所有项目生成 `context.json`，并根据语言生成对应的源码索引。`.agentsmd/` 保存中间分析结果，建议保留以便后续增量更新。

## 单独使用分析工具

以下示例使用 Bash / Zsh，在本仓库根目录执行。请将 `PROJECT_ROOT` 替换为待分析项目的绝对路径。

```bash
SKILL_DIR="$(pwd)"
PROJECT_ROOT="/absolute/path/to/your-project"
```

### 项目证据清单

```bash
python3 "$SKILL_DIR/scripts/projectcontext.py" \
  --root "$PROJECT_ROOT" \
  --out "$PROJECT_ROOT/.agentsmd/context.json"
```

输出 `files`、`categories`、`directories`、`stats` 和 `diagnostics`。同一文件可属于多个类别，例如 CI YAML 同时是配置；资产文件数与源码索引文件数的统计口径不同。`package.json` 事实用 JSON Pointer 定位，`go.mod` 和构建目标候选带行号。其他 manifest 仍需直接阅读；入口分类不代表已确认的运行入口。

扫描保留 `.github/workflows` 与 `.circleci`，排除依赖、常见构建产物及其他隐藏目录。单文件默认最多读取 2 MB，超限、解析失败和未跟随的符号链接记录在 `diagnostics`。环境文件只输出键名和位置，文档中也应避免写出凭据。

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

输出包含 `schema_version`、`root`、`packages`、`files`、`stats` 和 `diagnostics`。文件记录真实总行数、内容哈希、生成代码标记及构建条件；签名保留泛型、数组长度、函数类型和通道方向。分析器跳过隐藏子目录及 `vendor`、`node_modules`；解析失败的文件会输出警告并记录诊断。

识别到相关内容时还有 `routes` / `crons` 候选，保留接收者、handler 链和来源；这些结果必须核对注册逻辑、路径前缀与启用条件，不能直接作为已启用接口清单。

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

支持 TypeScript、JavaScript、Python、Java、Kotlin、Rust、C/C++、SQL、Protocol Buffers 和 Shell。Python 使用标准库 AST，提取精确声明范围、docstring、类方法、参数/返回注解和 imports；其他语言使用正则，只记录候选声明行（`line_quality: approximate`），不估算函数体范围，`exported: null` 表示未知。语法错误等问题记录在 `diagnostics`，重要行为仍需源码核对。

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

`callgraph.py` 默认使用索引的项目根，支持根目录与嵌套 `go.mod`；只将完整 import 路径匹配到实际索引包，不用目录后缀猜测内部依赖。`--module example.com/your-project` 可覆盖根 module，`--group-depth` 可调整目录聚合深度。输出是 **import 级依赖图**，实际调用需要继续阅读源码。热点函数跨度按起止行包含边界计算，只有声明行的正则符号不计算函数规模。

### 校验导览文档

编写好 `AGENTS*.md` 后执行：

```bash
python3 "$SKILL_DIR/scripts/verify.py" --root "$PROJECT_ROOT"
```

所有警告也需要判为失败时，加上 `--strict`（显式统计不一致始终为错误）：

```bash
python3 "$SKILL_DIR/scripts/verify.py" --root "$PROJECT_ROOT" --strict
```

默认读取目标项目中存在的 `.agentsmd/ast.json` 和 `.agentsmd/index.json`，混合语言项目自动检查两份。也可以重复 `--ast` 显式选择索引（路径相对当前工作目录）：

```bash
python3 "$SKILL_DIR/scripts/verify.py" \
  --root "$PROJECT_ROOT" \
  --ast "$PROJECT_ROOT/.agentsmd/index.json"
```

校验器递归扫描 `AGENTS*.md`（忽略依赖、构建产物和隐藏目录），链接相对所在 Markdown 解析。行号标签使用项目根相对路径，例如根文档中的 `[src/a.go:L10-L20](src/a.go#L10-L20)`；若文档在 `docs/`，目标应改为 `../src/a.go#L10-L20`。语句与配置行也允许作为证据；检查范围合法性及标签/目标一致性，不通过同名文件后缀猜测路径。代码围栏内的示例不参与引用校验。

默认也会检查已有协作规范和模板 `AGENTS.md`，可能提示缺少导览章节。只校验本次交付时，可重复使用 `--doc AGENTS.md --doc AGENTS.runtime.md`（路径相对项目根），保留无关模板原有指令。

显式统计标记按来源校验，避免把模块局部统计与项目总数混淆：

```html
<!-- agentsmd:stats {"source":".agentsmd/ast.json","file_count":12,"package_count":3,"symbol_count":48} -->
```

将数字替换为对应索引的实际值；混合项目可为两份索引分别添加标记。可见统计表仍需人工确认与标记一致。退出码 `0` 表示无错误，`1` 表示校验失败；警告需人工确认。源码哈希变化会导致索引过期错误，需要重建索引。

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

让 AI 助手结合报告复核并更新文档，然后重新执行 `verify.py`。确认完成后可删除备份文件。`diff.py` 只输出复核报告，不自动修改文档；新增文件即使没有文档引用，也需检查模块归属和根模块地图。文件内容哈希可发现签名和行数均未改变的实现变化；旧索引缺少哈希时会提示无法排除内容变化。

混合项目对 `index.json` 同样备份、重跑和比较；所有项目对 `context.json` 也执行同样流程，以捕获配置、CI、依赖和现有文档的变化。过滤范围、工具版本和解析失败也会影响完整性，不能仅凭“未发现变化”判断文档无需更新。

## 目录结构

```text
agentsmd-generator/
├── README.md
├── SKILL.md                         # AI 助手执行流程与生成约定
├── LICENSE                          # MIT 许可证
├── references/
│   └── module-doc-template.md       # 模块文档模板与行号引用规则
├── tests/test_analysis.py           # Python 场景回归测试
└── scripts/
    ├── goast/
    │   ├── go.mod
    │   ├── main.go                  # Go AST 分析器
    │   └── main_test.go             # Go 回归测试
    ├── codeindex.py                 # Python AST / 其他语言轻量索引
    ├── projectcontext.py            # 项目资产证据与哈希快照
    ├── hotspots.py                  # 规模热点报告
    ├── callgraph.py                 # import 级模块依赖报告
    ├── diff.py                      # 索引差异与文档影响报告
    └── verify.py                    # 导览文档校验器
```

## 使用边界

- **源码优先**：导览文档是导航辅助，代码变更后应重新分析并更新引用。工作流只新增或更新导览文档及分析产物，不修改业务源码。
- **行号与签名**：Go/Python 声明位置来自 AST，不进行类型检查和运行时求值；其他语言的声明行来自正则，不表示完整符号范围。Go 包含所有扫描到的构建变体，需要结合 build constraints 确定适用平台。
- **路由与 cron**：按语法模式识别，不会完整还原动态路由、路由组前缀、运行时调度或所有框架写法，需要人工复核。
- **校验不等于语义审查**：自动校验通过不代表功能描述正确，也不代表所有语言、链接和行号格式均被覆盖。
- **多语言辅助能力有限**：Python 可提取 imports，其余轻量规则仍可能遗漏多行、动态或嵌套声明；Go 导入关系报告不能用于推断非 Go 调用图。多语言统计按各索引分别列出，不把目录分组当作 Go 包。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
(cd scripts/goast && go test ./...)
```

测试覆盖配置/CI 发现、Python 方法和多行签名、混合索引、子目录链接、同名文件误引用、过期索引、函数体/配置变更以及嵌套 Go module。所有样例都在临时目录中创建。

## 许可证

本项目使用 [MIT License](LICENSE)。
