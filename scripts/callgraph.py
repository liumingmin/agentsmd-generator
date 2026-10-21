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
            directory_key = current.relative_to(root).as_posix()
            if current == root and module:
                prefix = module
            elif 'local_modules' in report:
                prefix = report['local_modules'].get(directory_key, '')
            else:
                prefix = detect_module(current)
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


def file_dependencies(report, root):
    """Return file import dependencies and unresolved imports, never runtime calls."""
    root = Path(root).resolve()
    files = {f['path']: f for f in report.get('files') or []}
    edges, unresolved = defaultdict(set), []
    packages = defaultdict(set)
    for path in files:
        if path.endswith('.go'): packages[Path(path).parent.as_posix()].add(path)
    go_edges = import_edges(report, root)
    js_exts = ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs')
    for path, file in files.items():
        directory = Path(path).parent.as_posix()
        if path.endswith('.go'):
            edges[path].update(packages[directory] - {path})  # implicit same-package references
            for target in go_edges.get(directory, []): edges[path].update(packages[target])
            continue
        for imp in file.get('imports') or []:
            candidates = []
            if Path(path).suffix in js_exts and imp.startswith('.'):
                base = (root / Path(path).parent / imp).resolve()
                if base.is_relative_to(root):
                    rel = base.relative_to(root).as_posix()
                    candidates = [rel] + [rel + ext for ext in js_exts] + [rel + '/index' + ext for ext in js_exts]
                    # NodeNext source commonly imports emitted .js paths from .ts source.
                    if base.suffix in ('.js', '.mjs', '.cjs'):
                        candidates += [str(Path(rel).with_suffix(ext)) for ext in ('.ts', '.tsx', '.mts', '.cts')]
            elif path.endswith('.py'):
                level = len(imp) - len(imp.lstrip('.'))
                base = root / Path(path).parent if level else root
                for _ in range(max(0, level - 1)): base = base.parent
                base = base / imp.lstrip('.').replace('.', '/')
                if base.resolve().is_relative_to(root):
                    rel = base.resolve().relative_to(root).as_posix()
                    candidates = [rel + '.py', rel + '/__init__.py']
            found = [candidate for candidate in candidates if candidate in files]
            if found:
                edges[path].add(found[0])
            else:
                unresolved.append({'file': path, 'import': imp, 'reason': 'external, alias, dynamic or outside indexed scope'})
    return edges, unresolved


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
