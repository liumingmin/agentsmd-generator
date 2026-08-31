---
name: agentsmd-generator
description: 分析项目代码与文档，生成项目级 AGENTS.md 说明文件，并为每个模块生成独立的模块说明文件（AGENTS.模块名.md），模块说明列出详细功能点及代码文件行号。内置 Golang AST 分析程序（scripts/goast）精确定位 Go 代码的包/文件/类型/函数/方法/结构体字段行号，另附多语言轻量索引脚本（scripts/codeindex.py，支持 TS/JS/Python/Java/Rust/C++/SQL/proto/shell）。触发词：生成 AGENTS.md / 生成 agents.md / 生成项目说明文件 / 分析项目生成模块文档 / 给项目写导航文档 / 生成模块说明并带行号 / 项目代码导览文档 / 更新 AGENTS.md。This skill should be used when the user wants to generate or update an AGENTS.md project guide with per-module docs containing feature lists and code line numbers.
---

# AGENTS.md 生成器（项目导览文档）

## Overview

为项目生成一套导览文档：根目录 `AGENTS.md` 作为总入口，概述项目定位、架构与构建方式，并链接到每个模块的说明文件 `AGENTS.<模块名>.md`；每个模块说明文件列出详细功能点，并标注关键符号所在代码文件与行号。

**语言策略**：Go 代码用 `scripts/goast`（完整 AST，行号精确，含结构体字段/接口方法/签名/tag）；其他语言用 `scripts/codeindex.py`（正则轻量索引，行号仅供参考，引用前必须人工核实）；不支持的语言只作定性描述。

## 执行逻辑（必须按顺序执行）

### Step 1 — 静态分析

**Go 项目 / 含 Go 的混合项目**（默认排除 vendor/.git/node_modules 等目录）：

```bash
# Windows
<skill_dir>/scripts/goast/goast.exe -root <项目根> -out <项目根>/.agentsmd/ast.json
# 源码方式（无 exe 或需跨平台时）
cd <skill_dir>/scripts/goast && go run . -root <项目根> -out <项目根>/.agentsmd/ast.json
```

若 `goast.exe` 不存在或平台不匹配，先 `go build -o goast.exe .`（需要 Go 1.21+）。纯 Go 标准库实现，无第三方依赖。

**非 Go 语言部分**（同一项目内的前端/脚本/SQL 等，或纯非 Go 项目）：

```bash
python <skill_dir>/scripts/codeindex.py --root <项目根或子目录> --out <项目根>/.agentsmd/index.json
# 限定语言：--ext .ts,.py
```

goast 输出 JSON：`packages`（目录→包→文件）、`files`（每文件 imports、symbols）、`routes`（HTTP 路由注册：方法/路径/处理函数/行号）、`crons`（cron 表达式及位置）。symbol 带 `kind`（func/method/struct/interface/type/const/var）、`line`/`end_line`、`doc`、`params`/`results`，struct/interface 额外带 `fields`（字段名/类型/行号/tag/是否嵌入）。codeindex 输出结构对齐但字段精简。**Go 符号行号只能取自 goast 输出；其他语言行号引用前必须打开文件核实。**

辅助脚本（Step 2/5/6 中调用）：

```bash
# 复杂度信号（大文件/大函数/大结构体 + 根文件热点表）
python <skill_dir>/scripts/hotspots.py --ast <项目根>/.agentsmd/ast.json
# 模块依赖图初稿（import 级聚合 + 被依赖排行）
python <skill_dir>/scripts/callgraph.py --ast <项目根>/.agentsmd/ast.json [--module <go module前缀>]
# 交付前自动校验（行号引用/字段引用/死链/统计一致性）
python <skill_dir>/scripts/verify.py --root <项目根> [--strict]
```

### Step 2 — 补充非代码信息

静态分析只覆盖符号，还需读取：

1. 依赖清单：`go.mod` / `package.json` / `requirements.txt` / `Cargo.toml` / `pom.xml`（模块名、版本、关键依赖）
2. 现有 `README.md` 与各级 `*.md`（提炼项目定位、用法、部署说明）
3. 入口文件（`main.go` / `cmd/` / `index.ts` / `manage.py` 等）与配置文件，归纳启动方式与外部依赖（DB、MQ、第三方服务）
4. SQL 迁移、proto、Makefile/Dockerfile/CI 配置等非代码资产，在相关模块备注中提及
5. **复杂度信号**：运行 `scripts/hotspots.py` 自动提取大文件/大函数/超大结构体，在模块文档"备注"中提示后续维护者；Top N 热点表直接用于根文件
6. **接口与调度清单**：HTTP 路由与 cron 任务直接取自 ast.json 的 `routes`/`crons`，写入相关模块文档，不再人工翻入口文件
7. **约定归纳**：抽样阅读 2-3 个核心文件，归纳错误处理、日志、命名、目录分层等风格约定（写进根 AGENTS.md）
8. **版本历史**：若存在 CHANGELOG.md 或 git 可用，提取近 3-5 条重大变更写入根文件（可选，能显著提升导览价值）

### Step 3 — 划分模块

以 `packages[].dir`（Go）或顶层目录（其他语言）为天然模块边界；目录过多时按业务域归并。每个模块对应一个说明文件。文件极少的包可合并到最近的业务模块并在文中说明。混合语言项目按"语言+业务域"双维度划分（如 `前端-web` / `后端-api` / `脚本-etl`）。

### Step 4 — 生成模块说明文件

按 `references/module-doc-template.md` 的模板，为每个模块写 `AGENTS.<模块名>.md`：

- **功能点清单**：按业务视角归纳功能点（不是逐函数罗列），每条功能点标注关键符号和 `[文件:L<起>-L<止>]` 行号区间
- **核心类型 / 对外接口**表格：类型、签名、说明、行号；Go 结构体可用 `fields` 摘关键字段及行号
- **依赖与协作**：从 imports 与调用关系归纳
- **备注**：大文件/大函数提示、deprecated 符号、TODO、测试覆盖入口
- 行号一律写 `file.go:L<n>` 形式；功能描述要结合 `doc` 注释与代码语义，不要照抄注释
- 非 Go 符号行号注明"约"（如 `file.ts:L120（约）`）或核实后再写

### Step 5 — 生成根 AGENTS.md

根文件包含：

1. **项目一句话定位** + 关键背景
2. **模块地图**：表格列出每个模块 → 链接到 `./AGENTS.<模块名>.md`，附一句话职责
3. **架构与数据流**：简述入口 → 各模块协作关系（可配 ASCII 图）
4. **构建/运行/测试**：从 README、Makefile、依赖清单归纳
5. **代码统计**：文件数/包数/符号数（取分析输出的 stats，多语言分行列出）
6. **约定**：包管理、错误处理、日志等项目级约定
7. **版本历史**（如有 CHANGELOG/git）：近几条重大变更
8. **其他文档索引**：项目已有的 README/设计文档/问题记录链接

### Step 6 — 校验

先跑自动校验，再人工复核清单：

```bash
python <skill_dir>/scripts/verify.py --root <项目根>   # 行号引用/字段引用/死链/统计一致性
```

verify.py 必须零错误通过（警告需逐条确认可接受）；然后人工确认：

- [ ] 每个模块文档都有：一句话职责、功能点清单（含行号）、依赖协作，三项缺一不可
- [ ] 功能点是业务视角归纳，不是函数清单复制
- [ ] 清理中间产物：询问用户是否保留 `.agentsmd/`（默认保留，便于增量更新）

## 增量更新（触发词"更新 AGENTS.md"走此路径）

diff 驱动，四步完成：

```bash
# 1. 备份旧分析
cp <项目根>/.agentsmd/ast.json <项目根>/.agentsmd/ast.old.json
# 2. 重跑 goast（代码已变动）
<skill_dir>/scripts/goast/goast.exe -root <项目根> -out <项目根>/.agentsmd/ast.json
# 3. 生成增量报告
python <skill_dir>/scripts/diff.py --old <项目根>/.agentsmd/ast.old.json --new <项目根>/.agentsmd/ast.json --root <项目根>
# 4. 按报告更新文档后校验
python <skill_dir>/scripts/verify.py --root <项目根>
```

diff.py 报告包含：符号新增/删除/行号偏移/签名变化、结构体字段变化、路由与 cron 变化，以及**受影响文档清单**——自动扫描 AGENTS*.md，列出引用了变化文件的具体行号位置（"第 N 行: [file.go:L<m>]"），按清单逐处复核更新即可，无需全文重读。

- 报告为"无实质变化"时直接结束，不动文档
- 行号偏移条目：更新文档中对应引用；签名变化条目：评估功能描述是否改写
- 更新完成后删除 `ast.old.json`，并更新根文件统计

## 边界

- Go 符号行号精确；其他语言行号为正则说明性结果，交付前必须核实
- 不修改任何项目源码，只新增/更新 `AGENTS*.md` 文档

## Resources

### scripts/goast/
Go AST 静态分析器（`main.go` + 预编译 `goast.exe`）。单文件标准库实现，可 `go build` 跨平台重编译。输出含结构体字段/接口方法明细、HTTP 路由注册、cron 表达式。用法见 Step 1。

### scripts/codeindex.py
多语言轻量索引器（纯标准库 Python 3）。支持 TS/JS/Python/Java/Kotlin/Rust/C/C++/SQL/proto/shell，输出与 goast 对齐的 JSON。正则实现，行号 best-effort。

### scripts/hotspots.py
复杂度信号提取：大文件/大函数/大结构体清单 + 根文件热点表（阈值可调）。

### scripts/callgraph.py
模块依赖图：聚合 imports 生成 ASCII 依赖边与"被依赖最多"排行。

### scripts/verify.py
交付前自动校验：行号引用命中、字段级引用、死链、统计一致性，零错误才能交付。

### scripts/diff.py
增量更新：对比新旧 ast.json，输出符号/字段/路由/cron 变化与受影响文档的引用位置清单。

### references/module-doc-template.md
模块说明文件模板与编写规则。生成模块文档前必须读取。
