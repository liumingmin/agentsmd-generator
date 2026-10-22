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


def validate(root, reports, docs, root_document=None):
    from schemas import report_errors
    errors = [error for source, report in reports.items() for error in report_errors(report, source)]
    warnings, checked = [], 0
    if errors: return errors, warnings, checked
    files, sources = {}, {}
    for source, report in reports.items():
        declared_root = report.get('root')
        if declared_root and Path(declared_root).resolve() != root:
            warnings.append(f'{source}: 索引 root 与项目根不同，请确认索引已重建')
        for notice in report.get('notices', []):
            warnings.append(f'{source}: {notice}')
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
    for file in files.values():
        for config in file.get('resolution_configs', []):
            try:
                _, current_hash = read_source(config['path'])
                if current_hash != config['content_hash']:
                    errors.append(f'{config["path"]}: resolution config changed; rebuild JS/TS index')
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f'resolution config unavailable: {exc}')
    for source, report in reports.items():
        if any(not f.get('content_hash') for f in report.get('files') or []):
            warnings.append(f'{source}: 旧索引缺少内容哈希，无法完整检查新鲜度')

    for md in docs:
        raw = (root / md).read_text(encoding='utf-8')
        text = without_fences(raw)
        import markdownlinks
        links, visible = markdownlinks.analyze(raw)
        for match in REF_PAT.finditer(visible):
            ref, start, end = match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
            checked += 1
            try:
                count, _ = read_source(ref)
                if not 1 <= start <= end <= count:
                    errors.append(f'{md}: {ref}:L{start}-L{end} 超出有效范围 1..{count}')
            except (OSError, ValueError) as exc:
                errors.append(f'{md}: 引用文件无法读取 {ref}: {exc}')
        for item in links:
            label, link = item.label, item.target
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
                for key in ('file_count', 'package_count', 'symbol_count', 'by_category'):
                    if key in spec and spec[key] != stats.get(key):
                        errors.append(f'{md}: {source} 的 {key} 文档值 {spec[key]} != 索引值 {stats.get(key)}')
            except (KeyError, ValueError, TypeError) as exc:
                errors.append(f'{md}: agentsmd:stats 标记无效: {exc}')
        from completeness import section_errors
        is_root = md == root_document if root_document is not None else Path(md).name.lower() == 'agents.md'
        warnings.extend(f'{md}: {message}' for message in section_errors(text, is_root))
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings)), checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--ast', action='append', help='索引路径，可重复；相对当前工作目录')
    parser.add_argument('--modules', help='module map; default discovered .agentsmd/modules.json')
    parser.add_argument('--evidence', help='evidence ledger; default discovered .agentsmd/evidence.json')
    parser.add_argument('--require-reviewed', action='store_true', help='require module review and behavioral evidence')
    parser.add_argument('--strict', action='store_true', help='所有警告也视为失败')
    from diagnostics import add_arguments
    add_arguments(parser)
    parser.add_argument('--delivery', choices=['draft', 'sampled', 'complete'], default='draft')
    args = parser.parse_args()
    args.require_reviewed = args.require_reviewed or args.delivery == 'complete'
    from diagnostics import render
    try:
        errors, warnings, mapping, ledger, stats = run_checks(args)
    except (OSError, ValueError, KeyError) as exc:
        errors = getattr(exc, 'errors', [str(exc)])
        warnings, mapping, ledger, stats = [], None, None, {}
    failed = bool(errors or (args.strict and warnings))
    if not args.json:
        print(f'[verify] 索引 {stats.get("index_count", 0)} 份，文档 {stats.get("document_count", 0)} 个，引用检查 {stats.get("checked_references", 0)} 处')
    result = render(errors, warnings, mapping, ledger, args, stats)
    if not args.json:
        print(f'[verify] {"FAILED" if failed else "OK"}: {result["error_count"]} 个错误，{result["warning_count"]} 个警告')
    raise SystemExit(int(failed))


def run_checks(args):
    root = Path(args.root).resolve()
    from guidewrites import check_pending
    check_pending(root)
    from artifacts import read_json, load_reports
    import snapshots
    selected = snapshots.manifest(root / '.agentsmd')
    pinned = {str((root / '.agentsmd').resolve()): selected}
    paths = args.ast or snapshots.paths(root / '.agentsmd', ('ast.json', 'index.json'), selected)
    if not paths:
        raise ValueError('未找到索引，请先运行 goast/codeindex.py')
    context_paths = snapshots.paths(root / '.agentsmd', ('context.json',), selected)
    all_reports = load_reports(root, list(dict.fromkeys(map(str, list(paths) + context_paths))), pinned=pinned)
    reports = {}
    for path, report in zip(list(dict.fromkeys(map(str, list(paths) + context_paths))), all_reports):
        p = Path(path).resolve()
        name = p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p)
        reports[name] = report
    module_path = Path(args.modules) if args.modules else root / '.agentsmd/modules.json'
    evidence_path = Path(args.evidence) if args.evidence else root / '.agentsmd/evidence.json'
    mapping = read_json(module_path) if module_path.exists() else None
    ledger = read_json(evidence_path) if evidence_path.exists() else None
    from schemas import require, mapping_errors, ledger_errors
    if module_path.exists(): require(mapping_errors(mapping))
    if evidence_path.exists(): require(ledger_errors(ledger))
    if mapping is not None:
        # A reviewed map selects delivered guides; unrelated instruction/template files keep their role.
        docs = sorted({m['document'] for m in mapping['modules']})
    else:
        docs = iter_docs(root)
    from guidepaths import validate_targets
    validate_targets(root, docs)
    missing_docs = [md for md in docs if not root_path(root, md).is_file()]
    docs = [md for md in docs if md not in missing_docs]
    if not docs:
        raise ValueError('未找到 AGENTS*.md 文档')
    root_document = next((m['document'] for m in mapping['modules'] if m['id'] == mapping['root_module']), None) if mapping else None
    errors, warnings, checked = validate(root, reports, docs, root_document)
    errors.extend(f'missing module document: {md}' for md in missing_docs)
    try:
        if mapping is not None:
            import modules
            errs, warns = modules.check(root, mapping, all_reports, require_reviewed=args.require_reviewed)
            errors.extend(errs); warnings.extend(warns)
            if evidence_path.exists():
                import evidence
                errs, warns = evidence.check(root, ledger, mapping, all_reports, args.require_reviewed)
                errors.extend(errs); warnings.extend(warns)
            elif args.require_reviewed:
                errors.append('required evidence ledger missing')
        elif args.modules or args.require_reviewed or evidence_path.exists():
            errors.append('module map missing; required for evidence/review checks')
        if args.evidence and not evidence_path.exists(): errors.append('specified evidence ledger missing')
        # Detect added/deleted/ignored sources as well as stale existing hashes.
        if args.require_reviewed:
            from schemas import reproducible_scope_errors
            for name, report in reports.items():
                errors.extend(reproducible_scope_errors(report.get('scope'), name + '.scope'))
        scoped = [report for report in reports.values() if report.get('scope', {}).get('selection_hash')]
        if scoped:
            from scanpolicy import current_selection
            seen_scopes = set()
            for report in scoped:
                scope_key = json.dumps(report['scope'], sort_keys=True)
                if scope_key in seen_scopes: continue
                seen_scopes.add(scope_key)
                current = current_selection(root, report['scope'])
                if current['scope'] != report['scope']:
                    errors.append('analysis selection is stale; rerun analyze.py and reconcile modules')
    except (OSError, ValueError, KeyError) as exc:
        errors.append(str(exc))
    return errors, warnings, mapping, ledger, {'index_count': len(paths), 'document_count': len(docs), 'checked_references': checked}


if __name__ == '__main__':
    main()
