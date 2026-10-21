#!/usr/bin/env python3
"""Compare code indexes or projectcontext snapshots, including file content.

Usage: python diff.py --old old.json --new new.json --root <project>
The tool reports evidence requiring review. It never decides that prose is
semantically up to date merely because symbols have the same signatures.
"""
import argparse
import json
import re
from pathlib import Path
from verify import REF_PAT, LINK_PAT, iter_docs, local_link_target, without_fences


def index_symbols(report):
    return {(f['path'], s['name'], s['kind']): s
            for f in report.get('files') or [] for s in f.get('symbols') or []}


def compare(old, new):
    old_files = {f['path']: f for f in old.get('files') or []}
    new_files = {f['path']: f for f in new.get('files') or []}
    changes = []
    unknown = []
    for path in sorted(old_files.keys() | new_files.keys()):
        before, after = old_files.get(path), new_files.get(path)
        reasons = []
        if before is None:
            reasons.append('新增文件')
        elif after is None:
            reasons.append('删除文件')
        else:
            if not before.get('content_hash') or not after.get('content_hash'):
                unknown.append(path)
            elif before['content_hash'] != after['content_hash']:
                reasons.append('内容变化（含函数体、注释或配置，需复核行为）')
            for key, label in [('imports', 'imports'), ('line_end', '文件行数'), ('size_bytes', '文件大小'),
                               ('facts', 'manifest 事实'), ('categories', '资产分类'),
                               ('build_constraints', '构建条件'), ('generated', '生成代码标记')]:
                if before.get(key) != after.get(key):
                    reasons.append(label + '变化')
            osym = {(s['name'], s['kind']): s for s in before.get('symbols') or []}
            nsym = {(s['name'], s['kind']): s for s in after.get('symbols') or []}
            for key in sorted(osym.keys() | nsym.keys()):
                a, b = osym.get(key), nsym.get(key)
                name = key[0]
                if a is None:
                    reasons.append(f'新增符号 {name}:L{b["line"]}')
                elif b is None:
                    reasons.append('删除符号 ' + name)
                elif a != b:
                    details = []
                    if (a.get('line'), a.get('end_line')) != (b.get('line'), b.get('end_line')):
                        details.append(f'行号 L{a["line"]} -> L{b["line"]}-L{b["end_line"]}')
                    for keys, label in [(('signature', 'params', 'results', 'exported'), '签名/可见性'),
                                        (('fields',), '字段（含类型/tag/行号）'), (('doc',), '注释')]:
                        if any(a.get(k) != b.get(k) for k in keys):
                            details.append(label)
                    reasons.append(f'符号 {name} 变化: ' + ('、'.join(details) or '索引元数据'))
        if reasons:
            changes.append({'path': path, 'reasons': reasons})
    for kind, label in [('routes', '路由候选'), ('crons', 'cron 候选')]:
        before = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in old.get(kind) or []}
        after = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in new.get(kind) or []}
        for value in sorted(before ^ after):
            entry = json.loads(value)
            changes.append({'path': entry['file'], 'reasons': [
                f'{label}{"新增/变更" if value in after else "删除/变更"}: {value}']})
    return changes, unknown


def affected_docs(root, changed):
    affected = {}
    for md in iter_docs(root):
        text = without_fences((root / md).read_text(encoding='utf-8'))
        hits = []
        for n, line in enumerate(text.splitlines(), 1):
            refs = {m.group(1).replace('\\', '/') for m in REF_PAT.finditer(line)}
            for match in LINK_PAT.finditer(line):
                target = local_link_target(md, match.group(2))
                if target is not None:
                    refs.add(Path(target).as_posix())
                    try:
                        refs.add((root / target).resolve().relative_to(root).as_posix())
                    except ValueError:
                        pass
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
    args = parser.parse_args()
    old = json.loads(Path(args.old).read_text(encoding='utf-8'))
    new = json.loads(Path(args.new).read_text(encoding='utf-8'))
    changes, unknown = compare(old, new)
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
