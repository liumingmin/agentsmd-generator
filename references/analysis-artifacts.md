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

整组结果写入不可变的 `snapshots/<snapshot_id>/`，每份报告带同一 snapshot_id，并保存 bundle 校验和。通过一次原子替换 `current.json` 发布；读取整组时固定一个版本，混合版本或校验和变化会报错。读取 selection、当前/上一代快照及 bundle 前检查结构；非法路径、字段类型或缺失校验和会报告错误，分析失败不替换既有结果。完整交付要求每份索引都有可重建的 scope（清单哈希、原始 options、有效 policy 和 Git ignore 状态）；完全缺少 scope 的旧索引仍可用于草稿检查，不能用于证明完整覆盖。写入锁防止两个发布者并发切换；锁随进程退出释放。源码或扫描清单在分析期间变化时，不发布新结果。

根目录的 `index.json` / `index.old.json` 等是配套工具可识别的逻辑引用文件，**不是报告正文**；所有配套读取工具同时兼容旧式真实 JSON。旧式报告在首次新发布时保留为上一版本，完整历史目录不自动删除。`modules.json` / `evidence.json` 始终独立维护，不随报告发布覆盖。

```bash
python3 <skill_dir>/scripts/snapshots.py list --dir <项目根>/.agentsmd
python3 <skill_dir>/scripts/snapshots.py show --dir <项目根>/.agentsmd --artifact index.json
python3 <skill_dir>/scripts/snapshots.py path --dir <项目根>/.agentsmd --artifact index.old.json
```

`show` 输出真实 JSON，`path` 输出可直接查看的不可变文件。不要用普通 json.load 解析逻辑引用。发布前被中断时保留上一整组；未发布版本可以留作诊断，不会自动当作当前结果。

`--out-dir` 可将分析结果放到项目外；此时其他命令需用 `--report` / `--ast` / `--map` / `--ledger` 等参数指定实际路径。项目内分析结果统一放 `.agentsmd`，防止将历史报告当作新输入。

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

`scope.options` 单独保存本次显式选项，例如 `{"exclude":["fixtures"],"respect_gitignore":null}`；null 表示采用当前项目配置，false 表示显式 `--no-gitignore`。`scope.policy` 保存默认值、当前 config 和显式选项合并后的实际策略。verify、测试输入快照和转正前检查均用 options 加当前配置重新选择文件，不将旧 policy 重放为命令行排除项。因此删除配置排除规则、改变 respect_gitignore/include_hidden 后，即使旧清单中的文件内容未变，也会发现范围变化。显式命令行排除及 --no-gitignore 仍保留。分析发布前（包括 --selection 输入）同样检查当前范围。

旧 scope 缺少 options 时，无法区分哪些排除项来自配置、哪些来自命令行；重新 analyze 并核对 modules.scope 与归属后再验证，不自动猜测或静默放行。旧测试执行快照继续视作历史记录，需要实际重跑才可绑定新范围。

统计中的 `by_category` 互斥：test 优先，其次 generated，再次 production。`is_test` / `generated` 独立保留；源文件计数与资产 categories（可多选）的计数不同。生产代码优先展示热点；测试与生成代码分别展示。

## 模块地图

统一入口只生成候选。审阅职责和入口、修改文档名后，将候选保存成 `.agentsmd/modules.json`；后续新候选应与该地图对照，不能直接覆盖人工结果。

抽样地图的精简示例（不是完整交付示例）：

```json
{
  "schema_version": 2,
  "delivery": {"mode":"sampled","scope_confirmed":true},
  "root": "/absolute/project",
  "root_module": ".",
  "scope": {"复制当前分析的完整scope": "此处仅为格式说明"},
  "modules": [
    {
      "id": ".", "parent": null, "document": "AGENTS.md",
      "children": ["storage"], "files": [],
      "review": {"scope":"unreviewed", "reviewed_files":[], "exclusions":[], "capabilities":[], "entrypoint": [], "implementation": [], "test": {"status": "not_reviewed", "reason": ""}}
    },
    {
      "id": "storage", "parent": ".", "document": "AGENTS.storage.md",
      "children": [], "files": ["storage/store.py", "storage/test_store.py"],
      "review": {
        "scope":"sampled", "reviewed_files":["storage/store.py"],
        "exclusions":[], "capabilities":[],
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

`review.scope` 为 unreviewed / sampled / complete，独立于测试状态。sampled 必须在可见文案出现“抽样/未完成”（或相应英文）；即使填写入口和一条结论也不能通过完整交付。对于 complete：

- 地图 delivery.mode 为 complete，scope_confirmed 为 true；后者是助手已核对模块边界的记录，不是要求用户额外批准。
- 每个归属文件恰好出现在 reviewed_files，或出现在 exclusions（对象含 path / reason）；入口与核心实现必须实际阅读，不能排除。
- 每项主要能力有唯一 id、title、status=reviewed、主要 claims，并交代 input / success / failure / configuration 四方面。
- covered 方面引用匹配的已确认 claim；not_applicable 填写原因；pending/unknown 不能作为完整交付。可执行能力至少有 covered 的成功结果。

能力清单片段：

```json
{
  "id":"save", "title":"保存记录", "status":"reviewed", "claims":["save-success"],
  "aspects":{
    "input":{"status":"covered","claims":["save-input"]},
    "success":{"status":"covered","claims":["save-success"]},
    "failure":{"status":"covered","claims":["save-failure"]},
    "configuration":{"status":"not_applicable","reason":"该入口无可变配置；已核对实现及依赖注入位置"}
  }
}
```

每条对应 claim 登记相同 module / capability（如 save）及单一 aspect。用不同结论解释校验、成功结果和错误分支，不能用一条成功说明同时填充失败方面。configuration 方面的 confirmed claim 使用 configuration kind，分别提供来源与读取实现的证据。纯接口/测试模块仍能使用 interface/test，不强制虚构运行流程。

`verify.py --require-reviewed` 等同于 `--delivery complete`；抽样使用 `--delivery sampled`，草稿可用默认 draft。旧地图无需自动迁移，可继续检查归属/引用，但完整交付前必须补齐上述字段。能力清单及阅读记录是审阅者声明，机器不能证明不存在尚未登记的业务功能。

阅读记录另含 `review.source_hashes`（根相对路径 → SHA-256）及 `recorded_at`。哈希覆盖模块所有直接归属源码，包括 reviewed_files 和 exclusions；只记录片段证据不足以冻结整个模块的阅读状态。例如原流程没有变化、同文件末尾新增功能时，既有 claim 可仍有效，但模块 complete 必须过期。

```bash
# 先阅读并更新能力清单/文案，再显式冻结已复核模块；可重复 --module
python3 <skill_dir>/scripts/modules.py record-review --root <项目根> --module storage
```

命令要求已声明 sampled/complete 且源码索引新鲜，只更新指定模块的源码哈希和记录时间，不提升阅读范围、不确认能力、不运行或改写测试结果，也不重捕获证据。旧地图可继续做草稿检查；缺失哈希时报告未冻结阅读，不能作为完整交付。source_hashes 内容变化或文件归属集合改变时，重新分析/capture 不会自动消除 stale，必须实际复核后再 record-review。文件排除理由仍需人工判断是否合理。

`review.test.status` 允许 `not_reviewed`、`not_run`、`not_found`、`not_applicable`、`passed`、`failed`。除未审阅外均需 `reason`，未发现时写搜索范围。通过/失败必须额外记录真实的执行信息和输入快照，推荐由显式执行工具生成：

```bash
# 仅在确实需要运行且命令已获授权时执行；脚本不猜测命令、不启动隐含测试
python3 <skill_dir>/scripts/teststate.py --root <项目根> --module storage --cwd . -- python3 -m unittest discover -s tests
# 共用一条测试命令覆盖多个已核对模块时，可重复 --module
```

工具要求索引新鲜，按参数数组执行（无隐式 shell），保存 command、argv、项目相对 cwd、UTC timestamp、真实 exit_code，并将执行前的 selection scope 和 source_hashes 写入 execution。快照保守覆盖扫描范围内的全部资产，包括源码、测试、配置、依赖/锁文件，另绑定 `.agentsmd/config.json`；不包含 AGENTS 文档或其他分析产物。执行后再次比较输入，输入变化则 inputs_unchanged=false，结果不能算当前版本已验证。失败命令同样保存记录，CLI 返回失败；不提升 review.scope、能力或结论确认状态。运行期间地图被修改则不覆盖该地图。

缺少输入指纹的旧记录，或源码、测试、配置、文件集合/扫描策略变化后的结果，只能视为历史结果。完整检查拒绝将其作为当前 passed/failed，草稿检查给出警告；record-review、重新分析、capture 均不刷新测试指纹。应实际重新执行，或将当前状态如实设为 not_run 并在 reason 中交代历史结果和未运行原因。环境、外部服务和未扫描的安装依赖不在此快照保证内；没有测量覆盖率则不提供覆盖率结论。校验器不会运行测试或独立证明手填执行记录真实，也不为生成文档强制执行有外部副作用的集成测试。

## 结论与证据

可见文案用唯一 ID 绑定，标记放在代码围栏外，不嵌套：

```markdown
<!-- agentsmd:claim storage-save -->
保存入口校验标识后调用持久化接口；存储失败向调用方返回错误。
证据：[api/save.py:L12-L20](api/save.py#L12-L20)、[storage/store.py:L5-L16](storage/store.py#L5-L16)。
<!-- agentsmd:endclaim -->
```

草稿证据示例（行号应替换成真实源码范围）：

```json
{"claims":[{
  "id":"storage-save", "module":"storage", "document":"AGENTS.storage.md",
  "kind":"flow", "status":"confirmed", "capability":"save", "aspect":"success",
  "evidence":[
    {"role":"caller","path":"api/save.py","line":12,"end_line":20},
    {"role":"callee","path":"storage/store.py","line":5,"end_line":16,"symbol_id":"func:save#0"}
  ]
}]}
```

每个区块的可见源码链接与 evidence 的路径、起止行集合必须一致，并包含匹配的行号锚点；多处角色可以共用同一范围。仅 JSON 有行号、正文漏链接或链接与证据清单不一致都会报错。

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

## 文档与证据框架

```bash
python3 <skill_dir>/scripts/draft.py --root <项目根> --map <已审阅地图.json>
```

默认预览位于 `.agentsmd/drafts/`，同时生成配套 modules.draft.json / evidence.draft.json。预览地图的 document 指向预览文件，final_document 保存最终目标，final_document_hash 保存初次预览时目标的 SHA-256（null 表示尚不存在）；preview.directory 及 preview.artifact_hashes 记录预览位置与最终 modules.json/evidence.json 的基线。重跑预览保留这些基线及人工内容，不用刷新哈希掩盖目标冲突。所有本地链接按实际文档目录计算。

再次运行默认生成命令时会读取该目录中已持久化的预览地图和 ledger，继续已有会话。即使默认输入仍是最终 modules.json，也使用预览的阅读/能力/测试记录与证据，不把它们降级为旧的最终记录；不重新计算目标基线。输入的模块 ID、最终路径、父子关系、归属和扫描范围需与会话相容，否则停止并要求核对合并。显式传入预览地图时保留该输入的审阅修改，但不能改写既有基线；同时提供 --ledger 可用于明确指定已核对的预览证据。未显式指定 --out-dir 时采用预览地图记录的目录。普通最终地图输入下继续会话不接受额外 --ledger，以免混用正式证据；应改用 --map 指向预览地图。

需要建立新基线时，从最终地图在未占用的目录创建新会话：

```bash
python3 <skill_dir>/scripts/draft.py --root <项目根> --new-preview --out-dir .agentsmd/drafts/next-review
```

新会话从当前最终文档初始化，不混用旧预览正文。目录已有文件时拒绝重置；已有正文但会话地图/ledger 缺失、损坏或不兼容时也不自动补建新基线，先保留并合并旧内容，或换用新空目录。恢复操作留下的空子目录不影响重新生成。生成期间会话元数据或正文发生并发修改时停止，不覆盖该编辑。

框架包含导航、仅直接归属的分类统计、源码行号候选及待核实文本，不把函数名改写成功能保证；所有新增证据为 unknown。候选先做词法分隔：Python 使用 tokenize；常见 C 风格语言排除行/块注释、引号字符串和原始字符串。JS/TS 必须具有 TypeScript AST 保存的 candidate_exclusions 与有效语法标记，按 UTF-16 偏移排除注释、字符串、正则字面量、整个模板及 JSX element/fragment/self-closing expression，不依赖斜杠上下文猜测正则。模板插值、JSX 文本、属性和内部处理器整体保守跳过；同一函数中这些表达式外的实际分支仍可给出候选。中文及非 BMP 字符不会改变行号定位。JS/TS 仅正则降级、旧索引缺少上述范围或语法无效时不提供自动候选，需重建 AST 或人工定位。

字面量保留为无语义的表达式占位，维持行号，因此真实的 `return "ok"` 或返回 JSX 仍可提供返回位置，但字面量中的 `throw`/`raise` 不提供失败位置。不支持的语言、未闭合注释/字符串或过期源码不提供自动候选。随后在归属的非测试、非生成实现中按 input/success/failure/configuration 分别匹配校验、返回/副作用、错误分支和配置读取信号，再按能力名称及已登记实现排序，最多给出三个位置。匹配不是语义证明。未匹配的方面保留空证据并说明需人工定位，不回退到首文件/首符号；显式登记的方面证据优先保留。

预览转正流程（路径均替换为实际绝对路径）：

```bash
# 完善预览正文和预览地图；阅读/测试记录命令也用 --map 指向这份预览地图
python3 <skill_dir>/scripts/evidence.py capture --root <项目根> --map <项目根>/.agentsmd/drafts/modules.draft.json --ledger <项目根>/.agentsmd/drafts/evidence.draft.json
python3 <skill_dir>/scripts/verify.py --root <项目根> --modules <项目根>/.agentsmd/drafts/modules.draft.json --evidence <项目根>/.agentsmd/drafts/evidence.draft.json --delivery sampled
python3 <skill_dir>/scripts/draft.py --root <项目根> --promote
python3 <skill_dir>/scripts/verify.py --root <项目根> --delivery sampled
# 完整交付应在转正前后都使用 --delivery complete，按需加 --strict
```

--promote 默认读取上述预览地图和 ledger；自定义预览位置用 --map / --ledger 显式指定。要求已核对模块边界、当前扫描范围和索引新鲜、预览 claim 与捕获记录一致。先检验原证据，再将人工预览正文写到 final_document，重定位父子/跨文档导航、源码链接及查询参数/行号锚点；围栏中的示例原样保留。只更新路径重写导致的 statement/statement_hash，不重新 capture 源码指纹，不改变 review、测试记录、claim.status 或 delivery.mode。尚未确认的草稿仍然是草稿，转正不代表完整审阅。

Markdown 链接按结构读取和定位，只替换实际 destination 的字符范围。显示标签、可选标题、引用 ID、围栏/行内/缩进代码、转义示例及注释保持原样。支持普通链接、图片链接、完整/折叠/快捷引用式链接、尖括号目标、平衡括号及嵌套图片链接；引用定义中被使用的目标迁移一次，未使用定义不改写。外部 URL 和纯锚点不迁移。导航、verify、证据检查与 diff 使用同一读取规则；引用式链接的定义可在 claim 外，校验仍按整篇文档解析。修改定义的目标/行号即使不改变 claim 正文哈希，也会被引用校验发现。行内代码里的示例不能作为实际源码证据。

最终文档及 `.agentsmd/modules.json`、`.agentsmd/evidence.json` 共用恢复批次。目标与初次预览基线不同且不等于本次输出时，在首次写入前报冲突；需先人工合并目标与预览，不能只改哈希跳过检查。预览在转正校验/暂存期间被编辑也会停止。完全相同的重复转正安全。旧预览缺少目标/基线时拒绝猜测；先核对最终地图，再从该地图重新生成预览并保留、合并既有正文和证据。--write 不接受预览地图，避免看似转正却仍写在 drafts 中。

登记能力清单后，可在方面项的 evidence 中提供更准确的候选 refs；否则工具从入口/实现候选中选取范围，仅用于引导阅读。它不推断 caller/callee、配置优先级或执行结果。无直接源码的纯概览不会凭空生成四条行为草稿。

核对模块边界并写入 scope_confirmed 后，`--write` 更新最终路径的 `agentsmd:draft-navigation` 导航和统计管理区。语义章节只在不存在时初始化，识别标题别名及 section 标记，之后保留人工正文与已存在的 claim 区块；不平衡/重复管理标记会报错。写入前对整份地图校验类型与所有目标，文件只能是 `AGENTS.md` / `AGENTS.<模块>.md`；重复路径（含大小写/Unicode 别名）、源码路径、分析存储/隐藏目录、依赖产物目录、符号链接和共享硬链接会被拒绝。预览目录只能是 `.agentsmd/drafts` 或其子目录，配套 JSON 目标同样禁止链接别名。发现无效目标时不写其他正常目标。

文档与配套 JSON 作为一批写入：先将旧内容与新内容暂存到 `.agentsmd/guide-write/<批次>/` 并保存校验和，再保存 active.json 恢复日志，最后逐文件替换。普通异常自动回滚；进程中断后恢复日志保留，生成和 verify 会停止并提示：

```bash
python3 <skill_dir>/scripts/draft.py --root <项目根> --recover
```

未完成批次恢复旧内容，并移除该批次新增文件；已标记 committed 的批次保留新内容并完成清理。恢复先检查全部目标及备份校验和，若有用户后续编辑或备份损坏则停止，不部分覆盖。用户应先将冲突编辑另存，恢复相应文件到批次前/后版本后重试。原文件权限保留；重复恢复安全。last.json 记录最近批次结果。写入期间其他读取者仍可能看到部分更新，此机制提供中断恢复，不宣称多个文件同时原子切换。

自动初始化的语义章节使用 `agentsmd:placeholder` / `agentsmd:endplaceholder`，新增待核实 claim 带 `agentsmd:draft-claim`。完整交付检查全部声明的文档，包括无直接源码的根/父概览；已确认的 claim 也不能保留这些标记。旧版“根据关键实现补充流程”等固定自动提示同样拦截；围栏、缩进及行内代码中的示例不参与此检查。实际未确认的外部行为可以作为有原因的 unknown 记录保留，不禁止一般“待核实”文字。应完成说明后替换整个占位区块，不能仅删标记来宣称已完成。

完整检查同时要求必要说明存在。根文档需要 purpose、architecture、commands；模块文档需要 responsibility、behavior、collaboration、tests。空标题、纯链接（包括图片及完整/折叠/快捷引用式链接和定义）、表格/生成导航、代码示例及单独 TODO 不算说明；明确说明不适用及原因可以通过。支持常见中英文标题，例如“项目定位”/Overview、“架构与数据流”/Architecture、“构建/运行/测试”/Getting started、“职责与目录”/Responsibilities、“主要功能”/Features、“依赖与协作”/Dependencies、“测试与验证”/Testing，子章节正文可以补充父章节。自定义标题或合并章节可在对应正文前放置一个或多个 `<!-- agentsmd:section architecture -->` 等标记；使用上列七个用途 ID。标记在代码区域外，与正文同属一个章节，生成器会识别并保留；行内代码中的标记示例不能声明章节用途。引用定义按整篇文档解析，即使位于章节外也不会计入说明正文。草稿/传统文档缺失说明时给出警告。此检查验证结构和非空正文，不评判语义是否正确或涵盖全部实现。

证据草稿仍单独保存，不覆盖正式 ledger，不自动 capture 或升级确认状态。阅读实现后重写待核实内容，再 capture 并校验；自动框架本身不是完整交付。

## 增量影响

`diff.py --old <旧.json> --new <新.json> --root <项目根> --json` 返回：

- `changes[].symbols`：稳定 ID、body/contract/location/doc/syntax/add/delete 类型和前后范围；重复声明按唯一契约或内容指纹区分，不覆盖同名符号；无法唯一配对时显式报告歧义，采用文件级保守复核。旧式序号 ID 仍可通过前后 ID 与范围辅助匹配。
- `conservative`：是否需要文件级复核；无指纹、顶层未索引变化、增删和契约变化不能只看局部范围。
- `impact.files` / `dependency_causes`：新旧 import 图上的反向传递影响，包含 Go 同包隐式依赖；Go module 标识随快照保存，避免路径重命名使旧边丢失。manifest 改动还会触发所在子树的保守复核。
- `impact.modules` / `claims`：模块文档和需要复核的登记结论；其他方法的独立实现变化可缩小范围。
- `unresolved_imports` / `scope_changed`：外部/别名/范围缺失等限制和扫描范围变化。

JS/TS 支持相对路径、index、reexport 和常见编译后 `.js` 指向 `.ts` 的定位；使用 TypeScript 编译器解析最近的 tsconfig/jsconfig（或 --tsconfig），支持明确配置的 paths/baseUrl 和本地 extends；解析配置及其哈希随快照保存。配置继承越出项目或目标未被扫描时明确报告限制，不猜测别名，不做类型级或动态调用解析。Python 解析基本 module import，复杂包导出需人工确认。每对语言索引单独比较，context 比较补充资产变化；跨语言运行时桥接不在 import 图内。

声明重排仍会触发文件级复核，因为初始化、装饰器或重复定义的行为可能依赖顺序；“只有位置变化”不等于语义不变。行号/格式变化也可能导致链接过期。根模块地图和统计、无引用的新增文件、未标记自由文本仍需检查。源码变更后先审阅文字，最后更新 ledger，避免盲目 capture 隐藏过期结论。

## 结构校验和诊断输出

模块地图、review、capabilities/aspects、证据清单及引用在遍历前检查对象/数组/字符串/整数等类型。错误定位示例为 `modules.$.modules[2].review.capabilities[0].aspects.failure.claims` 或 `evidence.$.claims[0].evidence[1].line`；布尔值不能当行号。结构无效时停止相关读取/写入，保留原文件；未知扩展字段保持兼容。字段校验不替代模块归属、证据范围和结论逻辑审查。

```bash
python3 <skill_dir>/scripts/verify.py --root <项目根> --delivery complete
python3 <skill_dir>/scripts/verify.py --root <项目根> --details
python3 <skill_dir>/scripts/verify.py --root <项目根> --json
# modules.py check 与 evidence.py check/capture 同样支持 --json / --details / --limit
```

默认输出按模块聚合，显示各类问题数量与代表性样例，默认最多 12 个模块组，`--limit` 调整。聚合只影响展示，不吞掉错误或改变退出码；`--details` 展开全部，`--json` 只输出 JSON（无混杂日志），包含 status、error_count、warning_count、groups 和全部 diagnostics。单条诊断含 severity、code、module、document、field、message，跨模块或尚未定位的错误归入项目组。`--strict` 下仅警告也返回失败，JSON status 同步反映结果。
