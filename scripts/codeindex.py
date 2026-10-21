#!/usr/bin/env python3
"""Multilanguage index: Python AST, optional TypeScript AST, explicit regex fallback."""
import argparse
import ast
import copy
import hashlib
import json
import os
import re
import sys
import io
import tokenize
import subprocess
from pathlib import Path
from scanpolicy import (classify, counts, digest, finalize_symbols, load_selection,
                        add_arguments, from_args)

# 每种语言: (扩展名集合, [ (kind, 正则, name组) ])
RULES = {
    "typescript": (
        {".ts", ".tsx", ".mts", ".cts"},
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
                symbol["content_hash"] = digest(ast.dump(node, include_attributes=False))
                contract = copy.deepcopy(node)
                if kind == "class":
                    for child in contract.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            child.body = []
                else:
                    contract.body = []
                symbol["signature_hash"] = digest(ast.dump(contract, include_attributes=False))
                if kind != "class":
                    symbol["signature"] = ast.dump(node.args, include_attributes=False) + (ast.dump(node.returns, include_attributes=False) if node.returns else "")
                    body = node.body
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                        body = body[1:]
                    symbol["body_hash"] = digest("\n".join(ast.dump(part, include_attributes=False) for part in body))
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
            if node.module is None:
                imports.extend("." * node.level + alias.name for alias in node.names if alias.name != "*")
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
        "top_level_hash": digest(ast.dump(ast.Module(body=[n for n in ast.parse(text).body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], type_ignores=[]), include_attributes=False)) if lang == "python" else None,
        "symbols": finalize_symbols(symbols),
        **classify(rel, text),
        "parser": {"name": "python-ast" if lang == "python" else "regex"},
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


def scan(root, selection=None, allow=None, js_parser="auto", typescript=None, node="node"):
    root = Path(root).resolve()
    selection = load_selection(root, selection)
    files, diagnostics, notices = [], list(selection["diagnostics"]), []
    paths = [p for p in selection["files"] if Path(p).suffix.lower() in EXT2LANG
             and (not allow or Path(p).suffix.lower() in allow)]
    js_paths = [p for p in paths if EXT2LANG[Path(p).suffix.lower()] in {"typescript", "javascript"}]
    parsed_js = False
    if js_paths and js_parser != "regex":
        payload = {"root": str(root), "files": js_paths, "typescript": typescript}
        try:
            run = subprocess.run([node, str(Path(__file__).with_name("jsindex.cjs"))],
                                 input=json.dumps(payload), text=True, capture_output=True, timeout=180)
            result = json.loads(run.stdout) if run.stdout else {}
            if run.returncode:
                raise ValueError(result.get("unavailable") or run.stderr or "JS parser failed")
            diagnostics.extend(result.get("diagnostics", []))
            parsed_files = []
            for file in result["files"]:
                file.update(classify(file["path"], (root / file["path"]).read_text(encoding="utf-8-sig")))
                finalize_symbols(file["symbols"])
                parsed_files.append(file)
            files.extend(parsed_files)
            parsed_js = True
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if js_parser == "typescript":
                raise ValueError(f"TypeScript AST required: {exc}") from exc
            notices.append(f"JS/TS regex fallback: {exc}")
    for rel in paths:
        lang = EXT2LANG[Path(rel).suffix.lower()]
        if parsed_js and rel in js_paths:
            continue
        try:
            files.append(scan_file(root / rel, rel, lang, RULES[lang][1]))
        except (ValueError, SyntaxError, OSError) as exc:
            diagnostics.append({"path": rel, "reason": str(exc)})
    files.sort(key=lambda f: f["path"])
    groups = {}
    for file in files:
        groups.setdefault((file["package"], file["language"]), []).append(file["path"])
    packages = [{"dir": directory, "package": directory or ".", "language": language, "files": paths}
                for (directory, language), paths in sorted(groups.items())]
    return {"schema_version": 3, "root": str(root), "scope": selection["scope"],
            "packages": packages, "files": files,
            "stats": {"file_count": len(files), "package_count": len(packages),
                      "symbol_count": sum(len(f["symbols"]) for f in files), "by_category": counts(files)},
            "diagnostics": diagnostics, "notices": notices,
            "limitations": ["Syntax-only indexing; exports do not establish a runtime contract.",
                            "Regex files have declaration-only ranges and no semantic fingerprints."]}


def parser_arguments(ap):
    ap.add_argument("--js-parser", choices=["auto", "typescript", "regex"], default="auto")
    ap.add_argument("--typescript", help="local TypeScript compiler module directory or typescript.js")
    ap.add_argument("--node", default="node")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--out")
    ap.add_argument("--ext", default="", help="extension whitelist, e.g. .ts,.py")
    add_arguments(ap)
    parser_arguments(ap)
    args = ap.parse_args()
    allow = {"." + e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}
    try:
        report = scan(args.root, from_args(args), allow, args.js_parser, args.typescript, args.node)
    except ValueError as exc:
        ap.error(str(exc))
    data = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(data, encoding="utf-8")
        print(f"codeindex: {report['stats']['file_count']} files -> {args.out}", file=sys.stderr)
    else:
        print(data, end="")


if __name__ == "__main__":
    main()
