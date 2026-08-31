#!/usr/bin/env python3
"""codeindex: 多语言轻量静态索引器（正则实现，非完整 AST）。

对非 Go 语言（TS/JS/Python/Java/Kotlin/Rust/C/C++/SQL/proto/shell）扫描
顶层符号（类/函数/接口/结构体/表定义等），输出与 goast 对齐的 JSON。
行号为正则匹配的"仅供参考"值，复杂嵌套场景可能不准；Go 代码请用 goast。

用法:
    python codeindex.py --root <项目根> [--out <输出.json>] [--ext .ts,.py]
"""
import argparse
import json
import os
import re
import sys

# 每种语言: (扩展名集合, [ (kind, 正则, name组) ])
RULES = {
    "typescript": (
        {".ts", ".tsx"},
        [
            ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"), 1),
            ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), 1),
            ("type", re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*="), 1),
            ("enum", re.compile(r"^\s*(?:export\s+)?enum\s+(\w+)"), 1),
            ("func", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"), 1),
            ("func", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), 1),
        ],
    ),
    "javascript": (
        {".js", ".jsx", ".mjs", ".cjs"},
        [
            ("class", re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), 1),
            ("func", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"), 1),
            ("func", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), 1),
        ],
    ),
    "python": (
        {".py"},
        [
            ("class", re.compile(r"^class\s+(\w+)"), 1),
            ("func", re.compile(r"^(?:async\s+)?def\s+(\w+)"), 1),
        ],
    ),
    "java": (
        {".java", ".kt"},
        [
            ("class", re.compile(r"^\s*(?:public\s+)?(?:abstract\s+)?(?:final\s+)?class\s+(\w+)"), 1),
            ("interface", re.compile(r"^\s*(?:public\s+)?interface\s+(\w+)"), 1),
            ("enum", re.compile(r"^\s*(?:public\s+)?enum\s+(\w+)"), 1),
        ],
    ),
    "rust": (
        {".rs"},
        [
            ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)"), 1),
            ("enum", re.compile(r"^\s*(?:pub\s+)?enum\s+(\w+)"), 1),
            ("trait", re.compile(r"^\s*(?:pub\s+)?trait\s+(\w+)"), 1),
            ("func", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"), 1),
            ("impl", re.compile(r"^\s*impl\s+(?:\w+\s+for\s+)?(\w+)"), 1),
        ],
    ),
    "c/c++": (
        {".c", ".h", ".cc", ".cpp", ".hpp"},
        [
            ("struct", re.compile(r"^\s*(?:typedef\s+)?struct\s+(\w+)"), 1),
            ("class", re.compile(r"^\s*class\s+(\w+)"), 1),
            ("func", re.compile(r"^[\w:*&<>]+\s+(\w+)\s*\([^;]*$"), 1),
        ],
    ),
    "sql": (
        {".sql"},
        [
            ("table", re.compile(r"(?i)^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)"), 1),
            ("view", re.compile(r"(?i)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+[`\"]?(\w+)"), 1),
        ],
    ),
    "proto": (
        {".proto"},
        [
            ("message", re.compile(r"^\s*message\s+(\w+)"), 1),
            ("service", re.compile(r"^\s*service\s+(\w+)"), 1),
            ("enum", re.compile(r"^\s*enum\s+(\w+)"), 1),
        ],
    ),
    "shell": (
        {".sh", ".bash"},
        [
            ("func", re.compile(r"^\s*(?:function\s+)?(\w+)\s*\(\s*\)\s*\{"), 1),
        ],
    ),
}

SKIP_DIRS = {".git", "vendor", "node_modules", ".workbuddy", "dist", "build", "__pycache__", "target"}

EXT2LANG = {}
for lang, (exts, _) in RULES.items():
    for e in exts:
        EXT2LANG[e] = lang


def scan_file(path, rel, lang, rules):
    symbols = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    for i, line in enumerate(lines, 1):
        for kind, rx, grp in rules:
            m = rx.match(line)
            if m:
                symbols.append({
                    "name": m.group(grp),
                    "kind": kind,
                    "exported": True,  # 正则无法可靠判断，统一 true
                    "line": i,
                    "end_line": i,  # 无法判定结束行，由使用方结合下一个符号推断
                })
                break
    return {
        "path": rel,
        "package": lang,
        "line_end": len(lines),
        "imports": [],
        "symbols": symbols,
        "is_test": bool(re.search(r"(_test|\.test|\.spec|Test)\.", os.path.basename(path))),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="")
    ap.add_argument("--ext", default="", help="逗号分隔的扩展名白名单，如 .ts,.py；缺省全部支持")
    args = ap.parse_args()

    allow = None
    if args.ext:
        allow = {e if e.startswith(".") else "." + e for e in args.ext.split(",")}

    root = os.path.abspath(args.root)
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            lang = EXT2LANG.get(ext)
            if not lang or (allow and ext not in allow):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            _, rules = RULES[lang]
            info = scan_file(full, rel, lang, rules)
            if info:
                files.append(info)

    files.sort(key=lambda x: x["path"])
    report = {
        "root": root.replace(os.sep, "/"),
        "packages": [],
        "files": files,
        "stats": {
            "file_count": len(files),
            "package_count": 0,
            "symbol_count": sum(len(f["symbols"]) for f in files),
        },
        "note": "regex-based index; line numbers are best-effort, verify before citing",
    }
    data = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(data)
        print(f"codeindex: {report['stats']['file_count']} files, "
              f"{report['stats']['symbol_count']} symbols -> {args.out}", file=sys.stderr)
    else:
        print(data)


if __name__ == "__main__":
    main()
