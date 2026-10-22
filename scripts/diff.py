#!/usr/bin/env python3
"""Compare code indexes or projectcontext snapshots, including file content.

Usage: python diff.py --old old.json --new new.json --root <project>
The tool reports evidence requiring review. It never decides that prose is
semantically up to date merely because symbols have the same signatures.
"""
import argparse
import json
from artifacts import read_json
import re
from pathlib import Path
from verify import REF_PAT, iter_docs, local_link_target


def symbols_by_id(file):
    from scanpolicy import finalize_symbols
    import copy
    symbols = copy.deepcopy(file.get('symbols') or [])
    if any(not s.get('id') for s in symbols):
        finalize_symbols(symbols)
    return {s['id']: s for s in symbols}


def match_symbols(before, after):
    """Match exact content, then unique contracts, then a single remaining declaration.

    Multiple unmatched declarations are intentionally left ambiguous. Declaration
    order is never evidence that two overloaded implementations are the same.
    """
    pairs, ambiguous = [], set()
    if before.get('symbols') == after.get('symbols'):
        old, new = symbols_by_id(before), symbols_by_id(after)
        return [((sid, old[sid]), (sid, new[sid])) for sid in old], ambiguous
    groups = {}
    for side, file in ((0, before), (1, after)):
        for sid, symbol in symbols_by_id(file).items():
            groups.setdefault((symbol['kind'], symbol['name']), [[], []])[side].append((sid, symbol))
    for key, (old, new) in sorted(groups.items()):
        for field in ('content_hash', 'signature_hash'):
            values = sorted({s.get(field) for _, s in old if s.get(field)} & {s.get(field) for _, s in new if s.get(field)})
            for value in values:
                left = [item for item in old if item[1].get(field) == value]
                right = [item for item in new if item[1].get(field) == value]
                if len(left) == len(right) == 1:
                    pairs.append((left[0], right[0]))
                    old.remove(left[0]); new.remove(right[0])
        same_content = {symbol.get('content_hash') for _, symbol in old + new}
        if len(old) == len(new) > 1 and len(same_content) == 1 and None not in same_content:
            pairs.extend(zip(sorted(old, key=lambda item: item[1]['line']), sorted(new, key=lambda item: item[1]['line'])))
            old, new = [], []
        if len(old) == len(new) == 1:
            pairs.append((old.pop(), new.pop()))
        if old and new:
            ambiguous.add(key)
        pairs.extend((item, None) for item in old)
        pairs.extend((None, item) for item in new)
    return pairs, ambiguous


def compare(old, new):
    old_files = {f['path']: f for f in old.get('files') or []}
    new_files = {f['path']: f for f in new.get('files') or []}
    changes, unknown = [], []
    for path in sorted(old_files.keys() | new_files.keys()):
        before, after = old_files.get(path), new_files.get(path)
        reasons, details, structural = [], [], False
        if before is None or after is None:
            reasons.append('新增文件' if before is None else '删除文件')
            structural = True
        else:
            if not before.get('content_hash') or not after.get('content_hash'):
                unknown.append(path)
            content_changed = before.get('content_hash') != after.get('content_hash')
            if content_changed:
                reasons.append('内容变化（含函数体、注释或配置，需复核行为）')
            osym, nsym = symbols_by_id(before), symbols_by_id(after)
            pairs, ambiguous = match_symbols(before, after)
            if ambiguous:
                structural = True
                reasons.append('ambiguous duplicate declarations; review without guessing identity')
            matched = [(i, old_item[1], new_item[1]) for i, (old_item, new_item) in enumerate(pairs)
                       if old_item and new_item]
            old_order = [i for i, a, b in sorted(matched, key=lambda item: (item[1]['line'], item[1]['name']))]
            new_order = [i for i, a, b in sorted(matched, key=lambda item: (item[2]['line'], item[2]['name']))]
            if old_order != new_order:
                structural = True
                reasons.append('声明顺序变化（需复核初始化、装饰器或重定义影响）')
            for old_item, new_item in pairs:
                a = old_item[1] if old_item else None
                b = new_item[1] if new_item else None
                sid = new_item[0] if new_item else old_item[0]
                kinds = []
                if a is None or b is None:
                    kinds.append('added' if a is None else 'deleted'); structural = True
                else:
                    if (a.get('line'), a.get('end_line')) != (b.get('line'), b.get('end_line')):
                        kinds.append('location')
                    if a.get('signature_hash') and b.get('signature_hash'):
                        contract = a['signature_hash'] != b['signature_hash'] or a.get('exported') != b.get('exported')
                    else:
                        contract = any(a.get(k) != b.get(k) for k in ('signature', 'params', 'results', 'exported', 'fields'))
                    if contract: kinds.append('contract'); structural = True
                    if a.get('body_hash') and b.get('body_hash') and a['body_hash'] != b['body_hash']:
                        kinds.append('body')
                    if a.get('doc') != b.get('doc'): kinds.append('doc')
                    if a.get('content_hash') != b.get('content_hash') and not any(k in kinds for k in ('contract', 'body', 'doc')):
                        kinds.append('syntax')
                if kinds:
                    symbol = b or a
                    details.append({'id': sid, 'before_id': old_item[0] if old_item else None,
                                    'after_id': new_item[0] if new_item else None, 'name': symbol['name'], 'kinds': kinds,
                                    'before': {'line': a['line'], 'end_line': a['end_line']} if a else None,
                                    'after': {'line': b['line'], 'end_line': b['end_line']} if b else None})
                    reasons.append(f"符号 {symbol['name']} 变化: {', '.join(kinds)}")
            # Parent class hashes include child bodies; avoid treating the whole class as
            # an independent implementation edit when only an indexed method changed.
            for detail in details:
                symbol = nsym.get(detail['after_id']) or osym.get(detail['before_id'])
                if symbol['kind'] == 'class' and 'syntax' in detail['kinds'] and any(
                    other['name'].startswith(detail['name'] + '.') and any(k in other['kinds'] for k in ('body', 'contract', 'added', 'deleted'))
                    for other in details):
                    detail['kinds'].remove('syntax')
            details = [d for d in details if d['kinds']]
            def contract_metadata(value):
                if isinstance(value, list): return [contract_metadata(item) for item in value]
                if isinstance(value, dict): return {k: contract_metadata(v) for k, v in value.items() if k not in ('line', 'end_line')}
                return value
            for key in ('imports', 'exports', 'facts', 'categories', 'build_constraints', 'generated', 'resolved_imports', 'resolution_configs'):
                if contract_metadata(before.get(key)) != contract_metadata(after.get(key)):
                    reasons.append(key + '变化'); structural = True
            for key in ('line_end', 'size_bytes'):
                if before.get(key) != after.get(key): reasons.append(key + '变化')
            if content_changed and not details:
                reasons.append('注释/格式或未索引代码变化；仅语法索引不能排除行为影响')
        if reasons:
            hashes_complete = bool(before and after and all(s.get('content_hash') for s in
                                   (before.get('symbols') or []) + (after.get('symbols') or [])))
            # A file may also contain executable top-level code. Ranges narrow symbol claims,
            # but whole-file and dependency review remains conservative.
            changes.append({'path': path, 'reasons': reasons, 'symbols': details,
                            'structural': structural, 'conservative': structural or not hashes_complete or not details or not before or not after or not before.get('top_level_hash') or before.get('top_level_hash') != after.get('top_level_hash')})
    for kind, label in [('routes', '路由候选'), ('crons', 'cron 候选')]:
        before = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in old.get(kind) or []}
        after = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in new.get(kind) or []}
        for value in sorted(before ^ after):
            entry = json.loads(value)
            changes.append({'path': entry['file'], 'reasons': [f'{label}变化: {value}'],
                            'symbols': [], 'structural': True, 'conservative': True})
    return changes, unknown


def impact(root, old, new, changes, unknown=(), mapping=None, ledger=None):
    from collections import defaultdict, deque
    from callgraph import file_dependencies
    edges, unresolved = file_dependencies(new, root)
    old_edges, old_unresolved = file_dependencies(old, root)
    reverse = defaultdict(set)
    for graph in (edges, old_edges):
        for source, targets in graph.items():
            for target in targets: reverse[target].add(source)
    changed = {c['path'] for c in changes} | set(unknown)
    reached, cause = set(changed), {}
    # Manifest changes can alter resolution/build behavior without touching source.
    # Scope this conservative review to that manifest's subtree rather than inventing call edges.
    from scanpolicy import SOURCE_EXTS
    universe = {f['path'] for report in (old, new) for f in report.get('files') or []}
    for path in sorted(changed):
        if Path(path).name in {'go.mod', 'go.work', 'package.json', 'pyproject.toml', 'tsconfig.json', 'jsconfig.json'}:
            directory = Path(path).parent
            for candidate in sorted(universe):
                if Path(candidate).suffix.lower() in SOURCE_EXTS and Path(candidate).is_relative_to(directory):
                    reached.add(candidate)
                    cause[candidate] = path + ' (manifest scope review)'
    queue = deque(sorted(reached))
    while queue:
        target = queue.popleft()
        for source in sorted(reverse[target]):
            if source not in reached:
                reached.add(source); cause[source] = target; queue.append(source)
    affected_modules = {}
    for module in (mapping or {}).get('modules', []):
        hits = reached & set(module.get('files', []))
        if hits: affected_modules[module['id']] = {'document': module['document'], 'files': sorted(hits)}
    claims = []
    by_path = defaultdict(list)
    for change in changes: by_path[change['path']].append(change)
    for claim in (ledger or {}).get('claims', []):
        reasons = []
        for ref in claim.get('evidence', []):
            path = ref['path']
            if path not in reached: continue
            if path in unknown or path not in changed:
                reasons.append(f'{path}: missing baseline hash or transitive import dependency')
                continue
            for change in by_path[path]:
                if change['conservative']:
                    reasons.append(f'{path}: file/contract/scope review')
                    continue
                for symbol in change['symbols']:
                    before = symbol.get('before')
                    overlaps = before and ref['line'] <= before['end_line'] and ref.get('end_line', ref['line']) >= before['line']
                    if ref.get('symbol_id') in {symbol['id'], symbol.get('before_id')} or overlaps:
                        reasons.append(f"{path}: {symbol['id']} {','.join(symbol['kinds'])}")
        if reasons: claims.append({'id': claim['id'], 'document': claim['document'], 'reasons': sorted(set(reasons))})
    return {'files': sorted(reached), 'dependency_causes': cause, 'modules': affected_modules, 'claims': claims,
            'unresolved_imports': unresolved + [x for x in old_unresolved if x not in unresolved],
            'limitations': ['Import reachability requests review; it does not prove runtime behavior.',
                            'Range filtering applies to symbol evidence; unindexed/top-level changes still need file review.']}


def affected_docs(root, changed):
    from bisect import bisect_right
    from collections import defaultdict
    import markdownlinks
    affected = {}
    for md in iter_docs(root):
        text = (root / md).read_text(encoding='utf-8')
        links, visible = markdownlinks.analyze(text)
        newlines = [i for i, char in enumerate(text) if char == '\n']
        linked = defaultdict(set)
        for link in links:
            target = local_link_target(md, link.target)
            if target is not None:
                refs = linked[bisect_right(newlines, link.start) + 1]
                refs.add(Path(target).as_posix())
                try: refs.add((root / target).resolve().relative_to(root).as_posix())
                except ValueError: pass
        hits = []
        for n, line in enumerate(visible.splitlines(), 1):
            refs = {m.group(1).replace('\\', '/') for m in REF_PAT.finditer(line)}
            refs.update(linked[n])
            matches = changed & refs
            if matches:
                hits.append((n, ', '.join(sorted(matches))))
        if hits:
            affected[md] = hits
    return affected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', required=True)
    parser.add_argument('--new', required=True)
    parser.add_argument('--root', default='.')
    parser.add_argument('--map', dest='mapping')
    parser.add_argument('--evidence')
    parser.add_argument('--json', action='store_true', help='structured report to stdout')
    args = parser.parse_args()
    import snapshots
    parents = {str(Path(path).parent.resolve()) for path in (args.old, args.new)}
    pinned = {parent: snapshots.manifest(parent) for parent in parents}
    old = snapshots.read_report(args.old, pinned[str(Path(args.old).parent.resolve())])
    new = snapshots.read_report(args.new, pinned[str(Path(args.new).parent.resolve())])
    changes, unknown = compare(old, new)
    root = Path(args.root).resolve()
    mapping_path = Path(args.mapping) if args.mapping else root / '.agentsmd/modules.json'
    evidence_path = Path(args.evidence) if args.evidence else root / '.agentsmd/evidence.json'
    mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else None
    ledger = json.loads(evidence_path.read_text()) if evidence_path.exists() else None
    effects = impact(root, old, new, changes, unknown, mapping, ledger)
    if args.json:
        print(json.dumps({'changes': changes, 'unknown': unknown, 'impact': effects,
                          'scope_changed': old.get('scope') != new.get('scope'),
                          'analyzer_changed': old.get('analysis_revision') != new.get('analysis_revision')}, ensure_ascii=False, indent=2))
        return
    changed = {c['path'] for c in changes}
    print('# 增量更新报告\n')
    print(f'变化文件 {len(changed)} 个；无法用哈希比较内容的文件 {len(unknown)} 个。')
    for change in changes:
        print('\n## ' + change['path'] + '\n')
        for reason in change['reasons']:
            print('- ' + reason)
    if changes:
        print('\n## 受影响文档与待复核引用\n')
        for md, hits in affected_docs(Path(args.root).resolve(), changed).items():
            print(f'- {md}: ' + '；'.join(f'第 {n} 行引用 {ref}' for n, ref in hits))
        print('\n同时复核根 AGENTS.md 的模块地图、数据流、命令和统计；新增/删除文件可能尚未被文档引用。')
    print('\n## 依赖影响与证据复核\n')
    print(f"直接及传递影响文件 {len(effects['files'])} 个；模块 {len(effects['modules'])} 个；结论 {len(effects['claims'])} 条。")
    for mid, item in effects['modules'].items(): print(f"- {mid} → {item['document']}")
    for claim in effects['claims']: print(f"- {claim['id']}: " + '; '.join(claim['reasons']))
    print(f"未解析 import {len(effects['unresolved_imports'])} 条（含外部依赖和别名），需结合源码判断。")
    if old.get('analysis_revision') != new.get('analysis_revision'):
        print('分析器实现版本变化：索引差异可能来自解析能力变化，请重建可比基线。')
    if old.get('scope') != new.get('scope'): print('扫描范围变化：先复核模块归属，再判断代码增量。')
    if unknown:
        print('\n部分文件未记录内容哈希（旧索引或扫描限制），无法排除内容变化；请结合源码/git diff 和 diagnostics 复核。')
    if old.get('diagnostics') or new.get('diagnostics'):
        print('\n分析存在 diagnostics，先确认漏扫或解析失败文件的影响。')
    if not changes:
        print('\n本次索引未发现变化；结论仅覆盖已扫描文件和已记录信息。')
    if not old.get('schema_version') or old.get('schema_version') != new.get('schema_version'):
        print('\n索引版本不同或旧版本未知，请确认扫描范围并重建基线。')


if __name__ == '__main__':
    main()
