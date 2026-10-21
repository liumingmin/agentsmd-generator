#!/usr/bin/env python3
"""codeindex: 多语言静态索引器（Python AST，其余语言正则）。

对非 Go 语言（TS/JS/Python/Java/Kotlin/Rust/C/C++/SQL/proto/shell）扫描
符号与目录分组，输出与 goast 对齐的 JSON。Python 用标准库 AST 精确提取，
其余语言只提供候选声明行，复杂嵌套场景可能遗漏；Go 代码请用 goast。

用法:
    python codeindex.py --root <项目根> [--out <输出.json>] [--ext .ts,.py]
"""
import argparse
import ast
import hashlib
import json
import os
import re
import sys
import io
import tokenize

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
            ("func", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"), 1),
        ],
    ),
    "javascript": (
        {".js", ".jsx", ".mjs", ".cjs"},
        [
            ("class", re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), 1),
            ("func", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"), 1),
            ("func", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"), 1),
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

SKIP_DIRS = {".git", "vendor", "node_modules", ".workbuddy", "dist", "build", "__pycache__", "target", "venv", "coverage"}

EXT2LANG = {}
for lang, (exts, _) in RULES.items():
    for e in exts:
        EXT2LANG[e] = lang


def python_index(text):
    """Use Python's parser for exact declaration ranges, signatures and imports."""
    tree = ast.parse(text, type_comments=True)
    symbols, imports = [], []

    def declarations(body, parent="", in_class=False):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = parent + node.name
                kind = "class" if isinstance(node, ast.ClassDef) else ("method" if in_class else "func")
                symbol = {"name": name, "kind": kind, "exported": not node.name.startswith("_"),
                          "line": node.lineno, "end_line": node.end_lineno,
                          "doc": ast.get_docstring(node) or "", "line_quality": "ast"}
                if kind != "class":
                    symbol["params"] = ast.unparse(node.args)
                    symbol["results"] = ast.unparse(node.returns) if node.returns else ""
                symbols.append(symbol)
                if kind == "class":
                    declarations(node.body, name + ".", True)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                # Module-level conditional definitions are still useful entry points.
                declarations(node.body, parent, in_class)
                declarations(getattr(node, "orelse", []), parent, in_class)
                declarations(getattr(node, "finalbody", []), parent, in_class)
                for handler in getattr(node, "handlers", []):
                    declarations(handler.body, parent, in_class)

    declarations(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append("." * node.level + (node.module or ""))
    return sorted(symbols, key=lambda s: (s["line"], s["name"])), sorted(set(imports))


def scan_file(path, rel, lang, rules):
    try:
        raw = open_bytes(path)
        encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0] if lang == "python" else "utf-8-sig"
        text = raw.decode(encoding)
        lines = text.splitlines(keepends=True)
    except OSError as exc:
        raise ValueError(f"cannot read {rel}: {exc}") from exc
    imports = []
    if lang == "python":
        symbols, imports = python_index(text)
    else:
        symbols = regex_index(lines, rules, lang)
    return {
        "path": rel,
        "package": os.path.dirname(rel).replace(os.sep, "/"),
        "language": lang,
        "line_end": len(lines),
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "imports": imports,
        "symbols": symbols,
        "is_test": bool(re.search(r"(^test_|_test\.|\.test\.|\.spec\.|Tests?\.)", os.path.basename(path)))
                   or any(p in {"test", "tests", "__tests__"} for p in rel.split("/")[:-1]),
        "line_quality": "ast" if lang == "python" else "approximate",
    }


def open_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def regex_index(lines, rules, lang):
    symbols = []
    in_comment = False
    for i, line in enumerate(lines, 1):
        if in_comment:
            if "*/" in line:
                in_comment = False
            continue
        if line.lstrip().startswith("/*"):
            in_comment = "*/" not in line
            continue
        if line.lstrip().startswith(("//", "#", "--")):
            continue
        for kind, rx, grp in rules:
            m = rx.match(line)
            if m:
                symbols.append({
                    "name": m.group(grp),
                    "kind": kind,
                    "exported": None,  # Export lists/macros cannot be resolved by regex.
                    "line": i,
                    "end_line": i,  # Declaration line only; do not invent a body span.
                    "line_quality": "approximate",
                })
                break
    return symbols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="")
    ap.add_argument("--ext", default="", help="逗号分隔的扩展名白名单，如 .ts,.py；缺省全部支持")
    args = ap.parse_args()

    allow = None
    if args.ext:
        allow = {"." + e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        ap.error(f"not a directory: {root}")
    files = []
    diagnostics = []
    def walk_error(exc):
        diagnostics.append({"path": str(exc.filename), "reason": str(exc)})

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            lang = EXT2LANG.get(ext)
            if not lang or (allow and ext not in allow):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            _, rules = RULES[lang]
            if os.path.islink(full):
                diagnostics.append({"path": rel, "reason": "symlink not followed"})
                continue
            try:
                info = scan_file(full, rel, lang, rules)
            except (ValueError, SyntaxError, OSError) as exc:
                diagnostics.append({"path": rel, "reason": str(exc)})
                continue
            if info:
                files.append(info)

    files.sort(key=lambda x: x["path"])
    groups = {}
    for f in files:
        groups.setdefault((f["package"], f["language"]), []).append(f["path"])
    packages = [{"dir": directory, "package": directory or ".", "language": language, "files": paths}
                for (directory, language), paths in sorted(groups.items())]
    report = {
        "schema_version": 2,
        "root": root.replace(os.sep, "/"),
        "packages": packages,
        "files": files,
        "stats": {
            "file_count": len(files),
            "package_count": len(packages),
            "symbol_count": sum(len(f["symbols"]) for f in files),
        },
        "diagnostics": diagnostics,
        "note": "Python uses ast; other languages use regex with declaration-only ranges and unknown exported flags. Verify semantics in source.",
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
