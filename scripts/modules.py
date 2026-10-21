#!/usr/bin/env python3
"""Plan hierarchical module candidates; check source ownership, review and navigation."""
import argparse
from pathlib import Path
from urllib.parse import unquote, urlsplit
import re
from artifacts import file_map, load_reports, read_json, write_json, safe_path
from scanpolicy import SOURCE_EXTS, counts, digest


def source_files(reports):
    return {p: f for p, f in file_map(reports).items()
            if 'source' in f.get('categories', []) or Path(p).suffix.lower() in SOURCE_EXTS}


def plan(root, reports, max_files=64, max_symbols=500):
    if max_files < 1 or max_symbols < 1:
        raise ValueError('module budgets must be positive')
    files = source_files(reports)
    modules = []
    unindexed = sorted(p for p, f in files.items() if 'symbols' not in f)

    def add(directory, members, parent=None):
        direct, groups = [], {}
        for path in members:
            remaining = Path(path).relative_to(directory).parts if directory != '.' else Path(path).parts
            if len(remaining) == 1:
                direct.append(path)
            else:
                child = (Path(directory) / remaining[0]).as_posix()
                groups.setdefault(child, []).append(path)
        measured = [files[p] for p in members if not files[p].get('generated')]
        oversized = len(measured) > max_files or sum(len(f.get('symbols') or []) for f in measured) > max_symbols
        split = bool(groups) and (directory == '.' or oversized)
        owned = sorted(direct if split else members)
        # Root + literal directory identity avoids slug collisions and remains editable.
        document = 'AGENTS.md'
        if directory != '.':
            document = f"AGENTS.{re.sub(r'[^A-Za-z0-9_-]+', '-', directory).strip('-') or 'module'}-{digest(directory)[:8]}.md"
        module = {'id': directory, 'parent': parent, 'document': document,
                  'children': sorted(groups) if split else [], 'files': owned,
                  'stats': counts(files[p] for p in owned),
                  'oversized': oversized, 'needs_manual_split': oversized and not split,
                  'review': {'entrypoint': [], 'implementation': [],
                             'test': {'status': 'not_reviewed', 'reason': ''}}}
        modules.append(module)
        if split:
            for child, paths in sorted(groups.items()):
                add(child, paths, directory)
    add('.', sorted(files))
    return {'schema_version': 1, 'root': str(Path(root).resolve()), 'status': 'candidate',
            'scope': reports[0].get('scope'), 'root_module': '.',
            'budgets': {'max_files': max_files, 'max_symbols': max_symbols},
            'source_count': len(files), 'modules': modules,
            'index_coverage': {'source_count': len(files), 'indexed_count': len(files) - len(unindexed),
                               'unindexed_files': unindexed},
            'limitations': ['Directory boundaries are candidates; review against actual responsibilities.',
                            'Ownership coverage does not mean source reading or test coverage.']}


def linked_documents(root, document):
    from verify import LINK_PAT, without_fences
    text = without_fences(safe_path(root, document).read_text(encoding='utf-8'))
    result = set()
    for match in LINK_PAT.finditer(text):
        parsed = urlsplit(match.group(2).strip('<>'))
        if not parsed.scheme and not parsed.netloc and parsed.path:
            target = (safe_path(root, document).parent / unquote(parsed.path)).resolve()
            if target.is_relative_to(root):
                result.add(target.relative_to(root).as_posix())
    return result


def check(root, mapping, reports, documents=True, require_reviewed=False):
    root = Path(root).resolve()
    errors, warnings = [], []
    if Path(mapping.get('root', '')).resolve() != root:
        errors.append('module map root mismatch')
    if mapping.get('scope') != reports[0].get('scope'):
        errors.append('module map scope is stale; reconcile source ownership')
    sources = source_files(reports)
    unindexed = sorted(p for p, f in sources.items() if 'symbols' not in f)
    if unindexed:
        warnings.append(f'{len(unindexed)} source files have no syntax index; review parser diagnostics/unsupported languages')
    if mapping.get('source_count', len(sources)) != len(sources):
        errors.append('module map source_count is stale')
    modules = mapping.get('modules', [])
    ids = [m['id'] for m in modules]
    docs = [m['document'] for m in modules]
    if len(set(ids)) != len(ids) or len(set(docs)) != len(docs):
        errors.append('duplicate module id or document')
    by_id = {m['id']: m for m in modules}
    owners = {}
    root_id = mapping.get('root_module')
    if root_id not in by_id or by_id[root_id].get('parent') is not None:
        errors.append('invalid root module')
    for module in modules:
        mid = module['id']
        parent = module.get('parent')
        if mid != root_id and (parent not in by_id or mid not in by_id[parent].get('children', [])):
            errors.append(f'{mid}: missing reciprocal parent link')
        visited, current = set(), mid
        while current in by_id and current != root_id:
            if current in visited:
                errors.append(f'{mid}: hierarchy cycle'); break
            visited.add(current)
            current = by_id[current].get('parent')
        for child in module.get('children', []):
            if child not in by_id or by_id[child].get('parent') != mid:
                errors.append(f'{mid}: invalid child {child}')
        for path in module.get('files', []):
            owners.setdefault(path, []).append(mid)
            if path not in sources:
                errors.append(f'{mid}: unknown/non-source owned file {path}')
        if module.get('needs_manual_split'):
            warnings.append(f'{mid}: exceeds budget without directory split; group by verified responsibility')
        review = module.get('review', {})
        missing = []
        for role in ('entrypoint', 'implementation'):
            refs = review.get(role, [])
            if module.get('files') and not refs:
                missing.append(role)
            for ref in refs:
                if ref not in module.get('files', []):
                    errors.append(f'{mid}: {role} review path is not owned: {ref}')
        test = review.get('test', {})
        status = test.get('status')
        if module.get('files') and status in (None, 'not_reviewed'):
            missing.append('test disposition')
        elif status not in ('not_reviewed', 'not_run', 'passed', 'failed', 'not_found', 'not_applicable'):
            errors.append(f'{mid}: invalid test status')
        if status not in (None, 'not_reviewed') and not test.get('reason'):
            errors.append(f'{mid}: test status needs a reason')
        if status in ('passed', 'failed'):
            execution = test.get('execution', {})
            if not execution.get('command') or not execution.get('cwd') or not execution.get('timestamp') or not isinstance(execution.get('exit_code'), int):
                errors.append(f'{mid}: test execution metadata missing')
            elif (execution['exit_code'] == 0) != (status == 'passed'):
                errors.append(f'{mid}: test result contradicts exit code')
        if missing:
            (errors if require_reviewed else warnings).append(f"{mid}: unreviewed {', '.join(missing)}")
        if documents:
            try:
                links = linked_documents(root, module['document'])
                required = module.get('children', []) + ([parent] if parent in by_id else [])
                for other in required:
                    if by_id[other]['document'] not in links:
                        errors.append(f'{mid}: missing navigation link to {other}')
            except (OSError, ValueError) as exc:
                errors.append(f'{mid}: document unavailable: {exc}')
    for path in sorted(sources):
        if path not in owners:
            errors.append(f'uncovered source: {path}')
    for path, mids in owners.items():
        if len(mids) != 1:
            errors.append(f"duplicate ownership: {path}: {', '.join(mids)}")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['plan', 'check'])
    ap.add_argument('--root', default='.')
    ap.add_argument('--report', action='append')
    ap.add_argument('--map', dest='mapping')
    ap.add_argument('--max-files', type=int, default=64)
    ap.add_argument('--max-symbols', type=int, default=500)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--no-docs', action='store_true')
    ap.add_argument('--require-reviewed', action='store_true')
    args = ap.parse_args()
    root = Path(args.root).resolve()
    path = Path(args.mapping) if args.mapping else root / '.agentsmd/modules.json'
    try:
        reports = load_reports(root, args.report)
        if args.command == 'plan':
            if path.exists() and not args.force:
                ap.error('module map exists; write candidates elsewhere or use --force')
            write_json(path, plan(root, reports, args.max_files, args.max_symbols))
            print(path)
        else:
            errors, warnings = check(root, read_json(path), reports, not args.no_docs, args.require_reviewed)
            for message in warnings: print('WARN ' + message)
            for message in errors: print('ERR ' + message)
            raise SystemExit(bool(errors))
    except (OSError, ValueError, KeyError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    main()
