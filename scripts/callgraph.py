#!/usr/bin/env python3
"""Go package import graph. Exact local module paths only, not a call graph.

Discovers the nearest go.mod for each indexed package, including nested modules.
Without a module declaration, no directory-suffix guesses are made.
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


def detect_module(root):
    path = Path(root) / 'go.mod'
    if path.is_file():
        match = re.search(r'(?m)^\s*module\s+([^\s]+)', path.read_text(encoding='utf-8'))
        if match:
            return match.group(1).strip('"')
    return ''


def import_edges(report, root, module=''):
    root = Path(root).resolve()
    packages = set()
    for file in report.get('files') or []:
        if file['path'].endswith('.go'):
            packages.add(Path(file['path']).parent.as_posix())
    mapping = {}
    for directory in sorted(packages):
        current = root / directory
        while current.is_relative_to(root):
            prefix = module if current == root and module else detect_module(current)
            if prefix:
                suffix = (root / directory).relative_to(current).as_posix()
                mapping[prefix + ('/' + suffix if suffix != '.' else '')] = directory
                break
            if current == root:
                break
            current = current.parent
    edges = defaultdict(set)
    for file in report.get('files') or []:
        if not file['path'].endswith('.go'):
            continue
        src = Path(file['path']).parent.as_posix()
        for imp in file.get('imports') or []:
            if imp in mapping and mapping[imp] != src:
                edges[src].add(mapping[imp])
    return edges


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ast', default='.agentsmd/ast.json')
    parser.add_argument('--root', help='默认使用索引 root')
    parser.add_argument('--module', default='', help='覆盖根 go.mod 的 module')
    parser.add_argument('--group-depth', type=int, default=2)
    args = parser.parse_args()
    report = json.loads(Path(args.ast).read_text(encoding='utf-8'))
    edges = import_edges(report, args.root or report.get('root', '.'), args.module)
    print('## 包依赖图（精确 import 匹配，不代表运行时调用）\n\n```')
    for src in sorted(edges):
        for dst in sorted(edges[src]):
            print(f'{src}  -->  {dst}')
    print('```')
    grouped = defaultdict(set)
    def group(path):
        return '/'.join(path.split('/')[:max(1, args.group_depth)])
    for src, targets in edges.items():
        for dst in targets:
            if group(src) != group(dst):
                grouped[group(src)].add(group(dst))
    print(f'\n## 模块依赖图（目录深度 {max(1, args.group_depth)}）\n\n```')
    for src in sorted(grouped):
        for dst in sorted(grouped[src]):
            print(f'{src}  -->  {dst}')
    print('```')
    reverse = defaultdict(set)
    for src, targets in edges.items():
        for dst in targets:
            reverse[dst].add(src)
    for dst, users in sorted(reverse.items(), key=lambda pair: (-len(pair[1]), pair[0]))[:5]:
        print(f'- `{dst}` 被 {len(users)} 个包导入: ' + ', '.join(sorted(users)))
    if not edges:
        print('\n未识别到内部导入边；检查 go.mod、扫描范围及是否为单包项目。')


if __name__ == '__main__':
    main()
