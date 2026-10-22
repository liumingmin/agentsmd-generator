#!/usr/bin/env python3
"""hotspots: 从 ast.json 提取复杂度信号，输出可直接粘贴进文档的 markdown。

信号：大文件（>500 行）、大函数（>100 行）、大结构体（>20 字段）。
阈值可用参数调整。

用法:
    python hotspots.py --ast .agentsmd/ast.json [--file 500] [--func 100] [--struct 20] [--top 8]
"""
import argparse
import json
from artifacts import read_json
from scanpolicy import classify


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ast", default=".agentsmd/ast.json")
    ap.add_argument("--file", type=int, default=500)
    ap.add_argument("--func", type=int, default=100)
    ap.add_argument("--struct", type=int, default=20)
    ap.add_argument("--top", type=int, default=8, help="热点表最多条目数")
    ap.add_argument("--category", choices=["all", "production", "test", "generated"], default="all")
    args = ap.parse_args()

    ast = read_json(args.ast)

    for category in ("production", "test", "generated"):
        if args.category not in ("all", category):
            continue
        selected = [f for f in ast["files"] if (f.get("category") or classify(f["path"], generated=f.get("generated", False))["category"]) == category]
        print(f"# {category}（{len(selected)} 个文件）\n")
        big_files, big_funcs, big_structs = [], [], []
        for f in selected:
            if f["line_end"] > args.file:
                big_files.append((f["line_end"], f["path"]))
            for s in f.get("symbols") or []:
                if s.get("line_quality") != "approximate" and s["kind"] in ("func", "method") and s["end_line"] - s["line"] + 1 > args.func:
                    big_funcs.append((s["end_line"] - s["line"] + 1, f["path"], s["line"], s["name"]))
                if s["kind"] == "struct" and len(s.get("fields", [])) > args.struct:
                    big_structs.append((len(s["fields"]), f["path"], s["line"], s["name"]))

        big_files.sort(reverse=True)
        big_funcs.sort(reverse=True)
        big_structs.sort(reverse=True)

        print(f"## 大文件（>{args.file} 行，共 {len(big_files)} 个）\n")
        for n, p in big_files:
            print(f"- [{p}]({p}): {n} 行")

        print(f"\n## 大函数（>{args.func} 行，共 {len(big_funcs)} 个）\n")
        for n, p, ln, name in big_funcs:
            print(f"- `{name}` [{p}:L{ln}]({p}): {n} 行")

        print(f"\n## 大结构体（>{args.struct} 字段，共 {len(big_structs)} 个）\n")
        for n, p, ln, name in big_structs:
            print(f"- `{name}` [{p}:L{ln}]({p}): {n} 字段")

        print("\n注：行数/字段数是规模信号，不代表性能或语义复杂度；未统计只有候选声明行的函数范围。")

        # 根文件用的热点汇总表：函数与结构体混排取 top N
        merged = [(n, f"`{name}` [{p}:L{ln}]({p})", f"{n} 行函数") for n, p, ln, name in big_funcs]
        merged += [(n, f"`{name}` [{p}:L{ln}]({p})", f"{n} 字段结构体") for n, p, ln, name in big_structs]
        merged.sort(reverse=True)
        print(f"\n## 复杂度热点表（Top {args.top}，供根 AGENTS.md 使用）\n")
        print("| 位置 | 规模 |")
        print("|------|------|")
        for n, loc, desc in merged[: args.top]:
            print(f"| {loc} | {desc} |")


if __name__ == "__main__":
    main()
