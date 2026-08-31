#!/usr/bin/env python3
"""diff: 对比新旧两份 ast.json，输出增量更新报告。

报告内容：
1. 符号变化 —— 新增 / 删除 / 行号偏移 / 签名(params/results)变化
2. 结构体字段变化 —— 字段新增 / 删除 / 行号偏移
3. 路由与 cron 变化
4. 受影响文档清单 —— 扫描 AGENTS*.md，列出哪些文档引用了变化的
   文件与行号区间，直接给出"需要人工复核的引用位置"

用法:
    # 代码变动后：先备份旧分析，再重跑 goast，最后 diff
    cp .agentsmd/ast.json .agentsmd/ast.old.json
    goast.exe -root . -out .agentsmd/ast.json
    python diff.py --old .agentsmd/ast.old.json --new .agentsmd/ast.json [--root .]
"""
import argparse
import glob
import json
import os
import re


def index_symbols(ast):
    """(file, name, kind) -> symbol"""
    out = {}
    for f in ast["files"]:
        for s in f.get("symbols") or []:
            out[(f["path"], s["name"], s["kind"])] = s
    return out


def sig(s):
    return (s.get("params", ""), s.get("results", ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--root", default=".")
    args = ap.parse_args()

    old = json.load(open(args.old, encoding="utf-8"))
    new = json.load(open(args.new, encoding="utf-8"))
    osym, nsym = index_symbols(old), index_symbols(new)

    added, removed, moved, sigchanged, fieldchanges = [], [], [], [], []

    for key, s in nsym.items():
        o = osym.get(key)
        if not o:
            added.append(key)
            continue
        if (s["line"], s["end_line"]) != (o["line"], o["end_line"]):
            moved.append((key, o["line"], s["line"], s["end_line"]))
        if sig(s) != sig(o):
            sigchanged.append(key)
        # 字段级对比
        of = {fd["name"]: fd for fd in o.get("fields", [])}
        nf = {fd["name"]: fd for fd in s.get("fields", [])}
        fadd = [n for n in nf if n not in of]
        fdel = [n for n in of if n not in nf]
        fmove = [n for n in nf if n in of and nf[n]["line"] != of[n]["line"]]
        if fadd or fdel or fmove:
            fieldchanges.append((key, fadd, fdel, fmove, nf))

    for key in osym:
        if key not in nsym:
            removed.append(key)

    # 路由 / cron 变化
    def route_key(r):
        return (r["method"], r["path"], r["handler"])
    oroutes = {route_key(r) for r in old.get("routes", [])}
    nroutes = {route_key(r) for r in new.get("routes", [])}
    ocrons = {(c["file"], c["expr"]) for c in old.get("crons", [])}
    ncrons = {(c["file"], c["expr"]) for c in new.get("crons", [])}

    print("# 增量更新报告\n")
    print(f"符号: 新增 {len(added)} / 删除 {len(removed)} / 行号偏移 {len(moved)} / 签名变化 {len(sigchanged)} / 字段变化 {len(fieldchanges)}\n")

    if added:
        print("## 新增符号\n")
        for f, n, k in sorted(added):
            s = nsym[(f, n, k)]
            print(f"- + `{n}` ({k}) [{f}:L{s['line']}]({f})")

    if removed:
        print("\n## 删除符号（文档中相关引用需移除）\n")
        for f, n, k in sorted(removed):
            print(f"- - `{n}` ({k}) {f}")

    if moved:
        print("\n## 行号偏移（文档引用需更新）\n")
        for (f, n, k), ol, nl, ne in sorted(moved):
            print(f"- `{n}` ({k}) {f}: L{ol} -> L{nl}-L{ne}")

    if sigchanged:
        print("\n## 签名变化（功能描述可能需改写）\n")
        for f, n, k in sorted(sigchanged):
            s = nsym[(f, n, k)]
            print(f"- `{n}` ({k}) [{f}:L{s['line']}]({f})  新签名: ({s.get('params','')}) -> ({s.get('results','')})")

    if fieldchanges:
        print("\n## 结构体字段变化\n")
        for (f, n, k), fadd, fdel, fmove, nf in sorted(fieldchanges):
            parts = []
            if fadd:
                parts.append("新增 " + ", ".join(f"`{x}`(L{nf[x]['line']})" for x in fadd))
            if fdel:
                parts.append("删除 " + ", ".join(f"`{x}`" for x in fdel))
            if fmove:
                parts.append("偏移 " + ", ".join(f"`{x}`->L{nf[x]['line']}" for x in fmove))
            print(f"- `{n}` [{f}]({f}): " + "；".join(parts))

    radd, rdel = nroutes - oroutes, oroutes - nroutes
    if radd or rdel:
        print("\n## 路由变化\n")
        for m, p, h in sorted(radd):
            print(f"- + {m} {p} -> {h}")
        for m, p, h in sorted(rdel):
            print(f"- - {m} {p} -> {h}")

    cadd, cdel = ncrons - ocrons, ocrons - ncrons
    if cadd or cdel:
        print("\n## cron 变化\n")
        for f, e in sorted(cadd):
            print(f"- + `{e}` ({f})")
        for f, e in sorted(cdel):
            print(f"- - `{e}` ({f})")

    # 受影响文档扫描
    changed_files = {k[0] for k in added + removed + sigchanged} | {m[0][0] for m in moved} | {fc[0][0] for fc in fieldchanges}
    if changed_files:
        os.chdir(args.root)
        docs = sorted(glob.glob("AGENTS*.md"))
        pat = re.compile(r"\[([\w./]+\.go):L(\d+)")
        print("\n## 受影响文档与待复核引用\n")
        any_hit = False
        for md in docs:
            hits = []
            for i, line in enumerate(open(md, encoding="utf-8"), 1):
                for m in pat.finditer(line):
                    ref, ln = m.group(1), int(m.group(2))
                    if any(ref == cf or cf.endswith(ref) for cf in changed_files):
                        hits.append((i, ref, ln))
            if hits:
                any_hit = True
                print(f"### {md}")
                for i, ref, ln in hits:
                    print(f"- 第 {i} 行: [{ref}:L{ln}]")
        if not any_hit:
            print("（无文档引用受影响的文件）")

    if not (added or removed or moved or sigchanged or fieldchanges or radd or rdel or cadd or cdel):
        print("\n**无实质变化，文档无需更新。**")


if __name__ == "__main__":
    main()
