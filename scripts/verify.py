#!/usr/bin/env python3
"""Validate guide references, local links, index freshness and scoped statistics.

Default inputs: .agentsmd/ast.json and .agentsmd/index.json (whichever exist).
Repeat --ast to select inputs explicitly. Labels use project-root paths, e.g.
[src/foo.go:L10-L20](src/foo.go#L10-L20). Link targets are relative to the doc.
Physical source ranges (including statements/config lines) are valid evidence;
matching a symbol start alone cannot establish correctness of a written claim.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

REF_PAT = re.compile(r'\[([^\]\n]+):L(\d+)(?:-L(\d+))?\]')
LINK_PAT = re.compile(r'\[([^\]\n]*)\]\((<[^>\n]+>|[^\s)]+)(?:\s+["\'][^\n]*?["\'])?\)')
FIELD_PAT = re.compile(r'`(\w+)\.(\w+)`\(L(\d+)\)')
STAT_PAT = re.compile(r'<!--\s*agentsmd:stats\s+(\{.*?\})\s*-->')
SKIP_DIRS = {'.git', '.agentsmd', 'node_modules', 'vendor', 'dist', 'build', 'target', '__pycache__', '.venv', 'venv'}


def iter_docs(root):
    docs = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
        for name in files:
            if re.fullmatch(r'(?i)agents(?:\..+)?\.md', name):
                docs.append((Path(current) / name).relative_to(root).as_posix())
    return sorted(docs)


def without_fences(text):
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append('\n')
        else:
            lines.append('\n' if fence else line)
    return ''.join(lines)


def root_path(root, ref):
    path = (root / ref.replace('\\', '/')).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'引用越出项目根目录: {ref}')
    return path


def local_link_target(md, link):
    link = link.strip('<>')
    parsed = urlsplit(link)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return Path(md).parent / unquote(parsed.path)


def validate(root, reports, docs):
    errors, warnings, checked = [], [], 0
    files, sources = {}, {}
    for source, report in reports.items():
        declared_root = report.get('root')
        if declared_root and Path(declared_root).resolve() != root:
            warnings.append(f'{source}: 索引 root 与项目根不同，请确认索引已重建')
        if report.get('diagnostics'):
            warnings.append(f'{source}: 有 {len(report["diagnostics"])} 个文件未完整分析，请检查 diagnostics')
        for file in report.get('files') or []:
            files.setdefault(file['path'], file)
            sources.setdefault(file['path'], set()).add(source)
    cache = {}

    def read_source(ref):
        if ref not in cache:
            path = root_path(root, ref)
            raw = path.read_bytes()
            cache[ref] = (len(raw.splitlines()), hashlib.sha256(raw).hexdigest())
        return cache[ref]

    for ref, file in files.items():
        try:
            count, digest = read_source(ref)
            if file.get('content_hash') and file['content_hash'] != digest:
                errors.append(f'{ref}: 索引已过期，源码内容已变化，请重建索引')
            elif 'line_end' in file and file['line_end'] != count:
                errors.append(f'{ref}: 索引行数 {file["line_end"]} 与源码 {count} 不一致，请重建索引')
        except (OSError, ValueError) as exc:
            errors.append(f'{ref}: 索引文件无法读取: {exc}')
    for source, report in reports.items():
        if any(not f.get('content_hash') for f in report.get('files') or []):
            warnings.append(f'{source}: 旧索引缺少内容哈希，无法完整检查新鲜度')

    for md in docs:
        text = without_fences((root / md).read_text(encoding='utf-8'))
        for match in REF_PAT.finditer(text):
            ref, start, end = match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
            checked += 1
            try:
                count, _ = read_source(ref)
                if not 1 <= start <= end <= count:
                    errors.append(f'{md}: {ref}:L{start}-L{end} 超出有效范围 1..{count}')
            except (OSError, ValueError) as exc:
                errors.append(f'{md}: 引用文件无法读取 {ref}: {exc}')
        for match in LINK_PAT.finditer(text):
            label, link = match.groups()
            target = local_link_target(md, link)
            if target is None:
                continue
            checked += 1
            path = root / target
            if not path.exists():
                errors.append(f'{md}: 死链 -> {link}')
                continue
            ref_match = re.fullmatch(r'(.+):L(\d+)(?:-L(\d+))?', label)
            fragment = urlsplit(link.strip('<>')).fragment
            if ref_match:
                try:
                    if root_path(root, ref_match.group(1)) != path.resolve():
                        errors.append(f'{md}: 引用标签与链接目标不一致: {label} -> {link}')
                    expected = 'L' + ref_match.group(2)
                    if ref_match.group(3):
                        expected += '-L' + ref_match.group(3)
                    if fragment and fragment != expected:
                        errors.append(f'{md}: 行号标签与锚点不一致: {label} -> {link}')
                except ValueError as exc:
                    errors.append(f'{md}: {exc}')
            elif re.fullmatch(r'L\d+(?:-L\d+)?', fragment):
                nums = [int(n) for n in re.findall(r'\d+', fragment)]
                try:
                    count = len(path.read_bytes().splitlines())
                    if not 1 <= nums[0] <= nums[-1] <= count:
                        errors.append(f'{md}: 链接锚点超出源码行数: {link}')
                except OSError as exc:
                    errors.append(f'{md}: {exc}')
        # Legacy field shorthand is checked only within files cited in the same row.
        for line in text.splitlines():
            refs = [m.group(1) for m in REF_PAT.finditer(line)]
            for typ, field, num in FIELD_PAT.findall(line):
                hits = [ref for ref in refs if any(
                    symbol.get('name') == typ and any(fd.get('name') == field and fd.get('line') == int(num)
                                                       for fd in symbol.get('fields') or [])
                    for symbol in files.get(ref, {}).get('symbols') or [])]
                if len(set(hits)) != 1:
                    warnings.append(f'{md}: 字段 {typ}.{field}(L{num}) 未在同一行引用的唯一文件中命中')
        for match in STAT_PAT.finditer(text):
            try:
                spec = json.loads(match.group(1))
                source = spec['source']
                stats = reports[source]['stats']
                for key in ('file_count', 'package_count', 'symbol_count'):
                    if key in spec and spec[key] != stats.get(key):
                        errors.append(f'{md}: {source} 的 {key} 文档值 {spec[key]} != 索引值 {stats.get(key)}')
            except (KeyError, ValueError, TypeError) as exc:
                errors.append(f'{md}: agentsmd:stats 标记无效: {exc}')
        headings = '\n'.join(re.findall(r'(?m)^#{1,6}\s+(.+)$', text))
        required = (['模块地图', '架构与数据流', '构建/运行/测试'] if Path(md).name.lower() == 'agents.md'
                    else ['功能点清单', '依赖与协作', '测试与验证'])
        missing = [section for section in required if section not in headings]
        if missing:
            warnings.append(f'{md}: 缺少建议章节: {", ".join(missing)}')
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings)), checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--ast', action='append', help='索引路径，可重复；相对当前工作目录')
    parser.add_argument('--strict', action='store_true', help='所有警告也视为失败')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    paths = args.ast or [str(p) for p in (root / '.agentsmd/ast.json', root / '.agentsmd/index.json') if p.exists()]
    if not paths:
        parser.error('未找到索引，请先运行 goast/codeindex.py')
    reports = {}
    for path in paths:
        p = Path(path).resolve()
        name = p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p)
        reports[name] = json.loads(p.read_text(encoding='utf-8'))
    docs = iter_docs(root)
    if not docs:
        parser.error('未找到 AGENTS*.md 文档')
    errors, warnings, checked = validate(root, reports, docs)
    print(f'[verify] 索引 {len(reports)} 份，文档 {len(docs)} 个，引用检查 {checked} 处')
    for warning in warnings:
        print('  WARN ' + warning)
    for error in errors:
        print('  ERR  ' + error)
    failed = bool(errors or (args.strict and warnings))
    print(f'[verify] {"FAILED" if failed else "OK"}: {len(errors)} 个错误，{len(warnings)} 个警告')
    raise SystemExit(int(failed))


if __name__ == '__main__':
    main()
