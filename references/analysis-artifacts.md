# 分析产物与校验协议

本文用于审阅模块地图、登记证据、运行交付检查和解释增量报告。所有源码路径相对同一项目根；文档中的 Markdown 目标仍相对文档所在目录。

## 扫描范围与运行环境

`analyze.py --root <绝对路径>` 生成：

| 文件 | 内容 |
| --- | --- |
| `selection.json` | 全部候选资产路径、排除策略、是否应用 Git ignore、清单哈希 |
| `context.json` | 资产类别、部分 manifest 事实、文件哈希、诊断 |
| `ast.json` | Go AST、候选路由/任务；仅含 Go 时存在 |
| `index.json` | Python、JS/TS、其他支持语言索引及解析能力声明 |
| `module-candidates.json` | 可调整的分层模块候选，阅读/测试记录初始为空 |
| `modules.json` | 人工审阅的模块归属、文档、层级和阅读记录 |
| `evidence.json` | 绑定可见文案的结论、证据角色和源码指纹 |

重跑自动保留一代 `*.old.json` 分析快照，不覆盖 `modules.json` / `evidence.json`。`--out-dir` 可将分析结果放到项目外；此时其他命令需用 `--report` / `--ast` / `--map` / `--ledger` 等参数指定实际路径。项目内分析结果统一放 `.agentsmd`，防止将历史报告当作新输入。

Python 与 Go 分析只依赖各自标准库。JS/TS AST 额外需要 Node 和本地 TypeScript 编译器；显式传入 `--js-parser typescript --typescript /path/to/typescript.js` 可使环境缺失直接失败。默认 `auto` 的降级原因保存在 `notices`；文件中的 `parser`、`line_quality` 是实际能力来源，不按扩展名假定使用 AST。解析错误记入 `diagnostics`，不会伪装成成功或降级为“没有符号”。

可选 `.agentsmd/config.json`：

```json
{
  "exclude": ["third_party", "fixtures/large/**"],
  "respect_gitignore": true,
  "include_hidden": [".custom-source"]
}
```

默认保留 `.github` / `.circleci`，排除其他隐藏子目录；隐藏根目录本身可以作为项目根。排除项不含 `/` 时匹配任一目录/文件名，含 `/` 时按根相对路径匹配，可用 glob；这不是完整 gitignore 语法。Git 跟踪文件即使命中 `.gitignore` 仍参与扫描，显式排除始终优先。符号链接不跟随。单独运行 Python 工具可使用同样的 `--selection`、`--exclude`、`--no-gitignore`；Go 使用 `-selection`，未指定时为兼容的 legacy walk，范围不等同于统一扫描。

统计中的 `by_category` 互斥：test 优先，其次 generated，再次 production。`is_test` / `generated` 独立保留；源文件计数与资产 categories（可多选）的计数不同。生产代码优先展示热点；测试与生成代码分别展示。

## 模块地图

统一入口只生成候选。审阅职责和入口、修改文档名后，将候选保存成 `.agentsmd/modules.json`；后续新候选应与该地图对照，不能直接覆盖人工结果。

精简示例：

```json
{
  "schema_version": 1,
  "root": "/absolute/project",
  "root_module": ".",
  "scope": {"复制当前分析的完整scope": "此处仅为格式说明"},
  "modules": [
    {
      "id": ".", "parent": null, "document": "AGENTS.md",
      "children": ["storage"], "files": [],
      "review": {"entrypoint": [], "implementation": [], "test": {"status": "not_reviewed", "reason": ""}}
    },
    {
      "id": "storage", "parent": ".", "document": "AGENTS.storage.md",
      "children": [], "files": ["storage/store.py", "storage/test_store.py"],
      "review": {
        "entrypoint": ["storage/store.py"],
        "implementation": ["storage/store.py"],
        "test": {"status": "not_run", "reason": "已阅读断言，本次未执行"}
      }
    }
  ]
}
```

从工具生成的候选保留真实 `root` / `scope`；示例不能直接用作目标数据。每个源文件恰好一个所有者；纯概览模块可没有直接文件，但不能代替子模块阅读。父子关系必须双向，文档也需互链。默认候选文档名带短哈希避免目录转连字符后重名，可审阅后改为易读名称。

```bash
python3 <skill_dir>/scripts/modules.py plan --root <项目根> --map <新的候选.json>
python3 <skill_dir>/scripts/modules.py check --root <项目根> --no-docs
python3 <skill_dir>/scripts/modules.py check --root <项目根> --require-reviewed
```

`plan` 默认不覆盖已有文件；不要对人工地图随意使用 `--force`。`--no-docs` 只适合写作前检查唯一归属。检查中的源集合来自资产清单和语言索引的并集，因此识别为源码但未支持语法的 `.ps1` / `.rb` 等也不能悄悄漏掉。候选的 `index_coverage` 列出未获语法索引的文件，归属覆盖不代表解析覆盖。

`review.test.status` 允许 `not_reviewed`、`not_run`、`not_found`、`not_applicable`、`passed`、`failed`。除未审阅外均需 `reason`，未发现时写搜索范围。通过/失败必须额外记录真实的执行信息：

```json
{"status":"passed","reason":"本次执行成功","execution":{
  "command":"python3 -m unittest discover -s tests",
  "cwd":"/absolute/project",
  "timestamp":"2026-10-21T10:00:00+08:00",
  "exit_code":0
}}
```

校验器检查记录一致性，不会运行测试或独立证明这段执行记录真实。不要为生成文档强制运行有外部副作用的集成测试。没有测量覆盖率则不提供覆盖率结论。

## 结论与证据

可见文案用唯一 ID 绑定，标记放在代码围栏外，不嵌套：

```markdown
<!-- agentsmd:claim storage-save -->
保存入口校验标识后调用持久化接口；存储失败向调用方返回错误。
<!-- agentsmd:endclaim -->
```

草稿证据示例（行号应替换成真实源码范围）：

```json
{"claims":[{
  "id":"storage-save", "module":"storage", "document":"AGENTS.storage.md",
  "kind":"flow", "status":"confirmed",
  "evidence":[
    {"role":"caller","path":"api/save.py","line":12,"end_line":20},
    {"role":"callee","path":"storage/store.py","line":5,"end_line":16,"symbol_id":"func:save#0"}
  ]
}]}
```

`symbol_id` 可选，来自索引；范围必须在对应符号内部。正则符号没有语法指纹时使用源码范围，不伪造符号哈希。`end_line` 缺省等于 `line`。确认后的证据可跨模块，但 behavior/flow 必须包含本模块拥有的 implementation/callee。

| kind | confirmed 需要的 role |
| --- | --- |
| behavior | implementation |
| flow | caller + callee |
| configuration | configuration + implementation |
| interface | interface |
| test | test（定位具体断言，执行状态另外记录） |

还可用 entrypoint 角色补充入口证据。每条主要能力分别记录，不用一条泛泛结论代表所有功能。纯声明模块可用拥有本模块接口证据的 interface 结论；纯测试模块可用本模块 test 证据满足主要结论要求，不必编造运行流程。`inferred` / `unknown` 必须有 `reason`，文案中也需出现“推断”/“待核实”（或相应英文）。无法确认时保留未知，不将其改成 confirmed 来消除警告。

```bash
python3 <skill_dir>/scripts/evidence.py capture --root <项目根> --draft <草稿.json>
python3 <skill_dir>/scripts/evidence.py check --root <项目根> --require-modules
python3 <skill_dir>/scripts/verify.py --root <项目根> --require-reviewed
```

`capture` 读取真实 Markdown 区块，记录 statement/hash、file_hash、snippet_hash 和可选 symbol_hash；只有结构检查无错误才写入。后续检查捕获文案改动、范围越界、过期片段、失效符号、缺失角色和不完整导航。文件其他位置的变化不会直接使片段过期，但索引必须保持最新；行号移动后仍需复核并重捕获。标记覆盖率只针对已登记结论，未标记自由文本和结论是否由证据逻辑推出仍需人工审查。

`verify.py` 自动发现模块地图/证据清单，也可用 `--modules` / `--evidence` 指定。显式统计按来源校验，例如：

```html
<!-- agentsmd:stats {"source":".agentsmd/ast.json","file_count":12,"package_count":3,"symbol_count":48} -->
```

可额外加入与报告一致的 `by_category` 对象。存在模块地图时，只检查地图声明的交付文档；仓库其他模板和指令文件不强制适用导览章节。仍需保留和遵守这些原有指令。

## 增量影响

`diff.py --old <旧.json> --new <新.json> --root <项目根> --json` 返回：

- `changes[].symbols`：稳定 ID、body/contract/location/doc/syntax/add/delete 类型和前后范围；重复声明以 `#序号` 区分，不覆盖同名符号。
- `conservative`：是否需要文件级复核；无指纹、顶层未索引变化、增删和契约变化不能只看局部范围。
- `impact.files` / `dependency_causes`：新旧 import 图上的反向传递影响，包含 Go 同包隐式依赖；Go module 标识随快照保存，避免路径重命名使旧边丢失。manifest 改动还会触发所在子树的保守复核。
- `impact.modules` / `claims`：模块文档和需要复核的登记结论；其他方法的独立实现变化可缩小范围。
- `unresolved_imports` / `scope_changed`：外部/别名/范围缺失等限制和扫描范围变化。

JS/TS 支持相对路径、index、reexport 和常见编译后 `.js` 指向 `.ts` 的定位；不读取 tsconfig aliases，不做类型级或动态调用解析。Python 解析基本 module import，复杂包导出需人工确认。每对语言索引单独比较，context 比较补充资产变化；跨语言运行时桥接不在 import 图内。

行号/格式变化也可能导致链接过期。根模块地图和统计、无引用的新增文件、未标记自由文本仍需检查。源码变更后先审阅文字，最后更新 ledger，避免盲目 capture 隐藏过期结论。
