#!/usr/bin/env python3
"""callgraph: 从 ast.json 的 imports 聚合模块间依赖，输出 ASCII 依赖图初稿。

只统计项目内部依赖（import 路径包含 --module 前缀，缺省自动取
go.mod 的 module 名；取不到则按"导入路径包含项目内目录名"启发式判断）。
依赖边按目标包所在目录归并为模块级。

用法:
    python callgraph.py --ast .agentsmd/ast.json [--module org/projectA] [--root .]
"""
import argparse
import json
import os
import re
from collections import defaultdict


def detect_module(root):
    gomod = os.path.join(root, "go.mod")
    if os.path.exists(gomod):
        for line in open(gomod, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*module\s+(\S+)", line)
            if m:
                return m.group(1)
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ast", default=".agentsmd/ast.json")
    ap.add_argument("--root", default=".")
    ap.add_argument("--module", default="", help="go module 前缀，缺省自动探测")
    args = ap.parse_args()

    ast = json.load(open(args.ast, encoding="utf-8"))
    module = args.module or detect_module(os.path.abspath(args.root))

    pkg_dirs = {p["dir"] for p in ast["packages"] if p["dir"]}
    # 顶层模块名归并：取目录前两段（如 projectA/module1/sub1 -> projectA/module1，默认取第一段+第二段视深度）
    def mod_of(dirpath):
        parts = dirpath.split("/")
        return "/".join(parts[:2]) if len(parts) > 2 else dirpath

    edges = defaultdict(set)  # src_mod -> {dst_mod}
    for f in ast["files"]:
        src_dir = os.path.dirname(f["path"])
        src = mod_of(src_dir) if src_dir else "(root)"
        for imp in f.get("imports") or []:
            # 内部依赖判断：模块前缀匹配，或路径尾部命中项目内目录
            dst_dir = ""
            if module and imp.startswith(module + "/"):
                dst_dir = imp[len(module) + 1:]
            else:
                for d in pkg_dirs:
                    if imp.endswith("/" + d) or imp == d:
                        dst_dir = d
                        break
            if not dst_dir:
                continue
            dst = mod_of(dst_dir) if dst_dir else "(root)"
            if dst != src:
                edges[src].add(dst)

    print("## 模块依赖图（import 级聚合，目录两段归并）\n")
    print("```")
    for src in sorted(edges):
        for dst in sorted(edges[src]):
            print(f"{src}  -->  {dst}")
    print("```")

    # 反向索引：被依赖最多的模块
    indeg = defaultdict(int)
    for src, dsts in edges.items():
        for d in dsts:
            indeg[d] += 1
    if indeg:
        print("\n被依赖最多（改动影响面大）：")
        for mod, n in sorted(indeg.items(), key=lambda x: -x[1])[:5]:
            print(f"- `{mod}`: 被 {n} 个模块依赖")


if __name__ == "__main__":
    main()
