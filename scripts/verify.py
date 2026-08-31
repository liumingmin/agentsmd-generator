#!/usr/bin/env python3
"""verify: AGENTS.md 文档质量自动校验。

校验项：
1. 行号引用 —— 文档中 [file.go:L<n>] / [file.go:L<n>-L<m>] 必须命中 goast
   输出中的符号或字段起始行；.sh/.sql 等非 Go 文件只检查文件存在。
2. 字段引用 —— 文中 `Type.Field`(L<n>) 形式的字段级引用，校验字段行号。
3. 死链 —— markdown 相对链接指向的文件必须存在。
4. 统计一致性 —— 根 AGENTS.md 中出现的"文件 N"/"符号 N"等数字与 ast.json
   stats 对比（宽松匹配，仅提示）。

用法:
    python verify.py --root <项目根> [--ast .agentsmd/ast.json] [--strict]
退出码: 0 全部通过 / 1 存在问题。
"""
import argparse
import glob
import json
import os
import re
import sys

REF_PAT = re.compile(r"\[([\w./\\]+\.(?:go|sh|sql|py|ts|js|java|rs|proto)):L(\d+)(?:-L(\d+))?\]")
LINK_PAT = re.compile(r"\]\(([^)\s#]+)\)")
FIELD_PAT = re.compile(r"`(\w+)\.(\w+)`\(L(\d+)\)")
GO_EXT = {".go"}


def build_index(ast):
    """path -> {'symbol_lines': set, 'fields': {(Type, Field): line}}"""
    idx = {}
    for f in ast["files"]:
        entry = idx.setdefault(f["path"], {"symbol_lines": set(), "fields": {}})
        for s in f.get("symbols") or []:
            entry["symbol_lines"].add(s["line"])
            for fd in s.get("fields", []):
                entry["symbol_lines"].add(fd["line"])
                entry["fields"][(s["name"], fd["name"])] = fd["line"]
    return idx


def find_path(idx, ref, line=None, extra=None):
    """按路径匹配 AST 文件；行号有效时优先用它消除歧义。"""
    if ref in idx:
        return ref
    ref = ref.replace("\\", "/")
    cand = [p for p in idx if p.endswith(ref)]
    if not cand:
        return None
    if len(cand) > 1 and line is not None:
        hit = [p for p in cand
               if line in idx[p]["symbol_lines"] or line in (extra or {}).get(p, set())]
        if len(hit) == 1:
            return hit[0]
        return None  # 仍有歧义或全不命中，交由上层逐个判断
    return cand[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--ast", default=None)
    ap.add_argument("--strict", action="store_true", help="统计不一致也视为失败")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    ast_path = args.ast or os.path.join(root, ".agentsmd", "ast.json")
    if not os.path.exists(ast_path):
        print(f"[verify] 未找到 {ast_path}，请先运行 goast", file=sys.stderr)
        sys.exit(1)

    ast = json.load(open(ast_path, encoding="utf-8"))
    idx = build_index(ast)

    os.chdir(root)
    docs = sorted(glob.glob("AGENTS*.md") + glob.glob("agents*.md"))
    if not docs:
        print("[verify] 未找到 AGENTS*.md 文档", file=sys.stderr)
        sys.exit(1)

    errors, warnings, checked = [], [], 0
    reported = set()  # (md, ref, line) 去重，同一引用在表格中多次出现只报一次

    # 路由/cron 提取的行号也是合法引用目标
    extra_lines = {}
    for r in ast.get("routes", []):
        extra_lines.setdefault(r["file"], set()).add(r["line"])
    for c in ast.get("crons", []):
        extra_lines.setdefault(c["file"], set()).add(c["line"])

    for md in docs:
        text = open(md, encoding="utf-8").read()

        # 1. 行号引用
        for m in REF_PAT.finditer(text):
            ref, l1 = m.group(1).replace("\\", "/"), int(m.group(2))
            checked += 1
            ref_norm = ref
            cands = [p for p in idx if p == ref_norm or p.endswith(ref_norm)]
            if cands:
                # 任一候选文件的符号/字段/路由行命中即视为有效
                valid = any(
                    l1 in idx[p]["symbol_lines"] or l1 in extra_lines.get(p, set())
                    for p in cands
                )
                if not valid and (md, ref, l1) not in reported:
                    reported.add((md, ref, l1))
                    tip = "" if len(cands) == 1 else f"（{ref} 匹配到 {len(cands)} 个文件，建议写全相对路径）"
                    errors.append(f"{md}: {ref}:L{l1} 无符号/字段/路由起始于该行{tip}")
            elif not os.path.exists(ref):
                if (md, ref) not in reported:
                    reported.add((md, ref))
                    errors.append(f"{md}: 引用文件不存在 {ref}")
            # 非 Go 且文件存在: 仅存在性检查通过

        # 2. 字段引用
        for m in FIELD_PAT.finditer(text):
            typ, fld, ln = m.group(1), m.group(2), int(m.group(3))
            found = any(e["fields"].get((typ, fld)) == ln for e in idx.values())
            if not found and (md, typ, fld, ln) not in reported:
                reported.add((md, typ, fld, ln))
                # 字段可能属于非 Go 文件或无 fields 输出，降级为警告
                warnings.append(f"{md}: 字段 `{typ}.{fld}`(L{ln}) 未在 AST 字段索引中命中")

        # 3. 死链
        for m in LINK_PAT.finditer(text):
            link = m.group(1)
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            checked += 1
            if not os.path.exists(link) and (md, link) not in reported:
                reported.add((md, link))
                errors.append(f"{md}: 死链 -> {link}")

    # 4. 统计一致性（仅根文件，宽松）
    root_doc = next((d for d in docs if d.upper() == "AGENTS.MD"), None)
    if root_doc:
        text = open(root_doc, encoding="utf-8").read()
        st = ast["stats"]
        for label, actual in (("文件", st["file_count"]), ("包", st["package_count"]), ("符号", st["symbol_count"])):
            nums = [int(n) for n in re.findall(label + r"\s*\*?\*?(\d+)\*?\*?\s*个", text)]
            if nums and actual not in nums:
                msg = f"{root_doc}: 统计\"{label}\" 文档值 {nums} 与实际 {actual} 不一致"
                (errors if args.strict else warnings).append(msg)

    print(f"[verify] 文档 {len(docs)} 个，引用检查 {checked} 处")
    for w in warnings:
        print(f"  WARN {w}")
    for e in errors:
        print(f"  ERR  {e}")
    if errors:
        print(f"[verify] FAILED: {len(errors)} 个错误, {len(warnings)} 个警告")
        sys.exit(1)
    print(f"[verify] OK ({len(warnings)} 个警告)")


if __name__ == "__main__":
    main()
