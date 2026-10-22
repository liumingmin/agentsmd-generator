#!/usr/bin/env python3
"""Generate navigation, measured statistics and explicitly unconfirmed claim drafts."""
import argparse
import copy
import os
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit
from artifacts import load_reports, file_map, read_json, write_json, safe_path
from citations import citation
from modules import ASPECTS
from guidepaths import validate_targets, preview_directory, contained_path, guide_path
from schemas import mapping_errors, ledger_errors, require
from scanpolicy import counts, digest
from candidates import candidate_refs

ASPECT_TITLES = {'input': '输入与校验', 'success': '成功结果与副作用',
                 'failure': '失败与边界分支', 'configuration': '配置来源与生效条件'}


def replace_managed(text, name, content):
    start, end = f'<!-- agentsmd:{name} -->', f'<!-- agentsmd:end{name} -->'
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise ValueError(f'unbalanced/duplicate managed {name} block')
    block = start + '\n' + content.strip() + '\n' + end
    if start not in text:
        return text.rstrip() + '\n\n' + block + '\n'
    pattern = re.escape(start) + r'.*?' + re.escape(end)
    return re.sub(pattern, lambda _: block, text, flags=re.S)


def relocate_links(root, text, source_doc, destination_doc, documents=None):
    from markdownlinks import rewrite
    def replace(target):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return None
        absolute = (safe_path(root, source_doc).parent / unquote(parsed.path)).resolve()
        if not absolute.is_relative_to(root):
            raise ValueError(f'link outside project: {target}')
        relative_target = absolute.relative_to(root).as_posix()
        if relative_target in (documents or {}): absolute = safe_path(root, documents[relative_target])
        relative = os.path.relpath(absolute, safe_path(root, destination_doc).parent).replace(os.sep, '/')
        link = quote(relative, safe='/') + (('?' + parsed.query) if parsed.query else '') + (('#' + parsed.fragment) if parsed.fragment else '')
        return link
    return rewrite(text, replace)


def generate(root, mapping, reports, ledger=None, write=False, out_dir=None, new_preview=False, inputs=None):
    require(mapping_errors(mapping))
    if ledger is not None: require(ledger_errors(ledger, draft=True))
    root = Path(root).resolve()
    from guidewrites import check_pending
    check_pending(root)
    dependencies = dict(inputs or {})
    if not write:
        out_dir = out_dir if out_dir is not None else mapping.get('preview', {}).get('directory', '.agentsmd/drafts')
        out_dir = Path(out_dir).as_posix()
        from previewstate import prepare
        mapping, ledger, session_inputs = prepare(root, mapping, ledger, out_dir, new_preview)
        if any(path in dependencies and dependencies[path] != raw for path, raw in session_inputs.items()):
            raise ValueError('preview input changed before generation; rerun')
        dependencies.update(session_inputs)
    if Path(mapping['root']).resolve() != root: raise ValueError('module map root mismatch')
    ids = [m['id'] for m in mapping['modules']]
    if len(set(ids)) != len(ids): raise ValueError('duplicate module id')
    validate_targets(root, [m['document'] for m in mapping['modules']])
    if not write: preview_directory(root, out_dir)
    if write and mapping.get('delivery', {}).get('scope_confirmed') is not True:
        raise ValueError('confirm module boundaries before writing final guide paths')
    output = copy.deepcopy(mapping)
    original_docs = {m['id']: m['document'] for m in mapping['modules']}
    if not write:
        if 'preview' not in output:
            output['preview'] = {'directory': out_dir, 'artifact_hashes': {}}
            for name in ('modules.json', 'evidence.json'):
                target = contained_path(root, '.agentsmd/' + name)
                output['preview']['artifact_hashes'][name] = digest(target.read_bytes()) if target.exists() else None
        output['preview']['directory'] = out_dir
        for module in output['modules']:
            final = module.get('final_document', module['document'])
            final_path = guide_path(root, final)
            if Path(final).parts[0] == '.agentsmd':
                raise ValueError('legacy preview lacks final target; regenerate from the reviewed final module map')
            if 'final_document' not in module:
                module['final_document'] = final
                module['final_document_hash'] = digest(final_path.read_bytes()) if final_path.exists() else None
            module['document'] = (Path(out_dir) / final).as_posix()
    elif 'preview' in output or any(Path(m['document']).parts[0] == '.agentsmd' for m in output['modules']):
        raise ValueError('preview map requires --promote; --write does not finalize preview paths')
    validate_targets(root, [m['document'] for m in output['modules']])
    destination = root / '.agentsmd' if write else preview_directory(root, out_dir)
    for name in ('modules.draft.json', 'evidence.draft.json'):
        contained_path(root, (destination / name).relative_to(root).as_posix())
    by_id = {m['id']: m for m in output['modules']}
    for module in output['modules']:
        neighbors = module.get('children', []) + ([module['parent']] if module.get('parent') else [])
        if any(mid not in by_id for mid in neighbors): raise ValueError(f'{module["id"]}: unknown parent/child')
    files = file_map(reports)
    claims = copy.deepcopy((ledger or {}).get('claims', []))
    # Existing evidence is carried forward; generation never promotes unknown claims.
    for claim in claims:
        if claim['module'] in by_id:
            claim['document'] = by_id[claim['module']]['document']
    known = {c['id']: c for c in claims}
    writes, originals, generated_ids, candidate_cache = {}, {}, set(), {}
    for module in output['modules']:
        mid, document = module['id'], module['document']
        path = guide_path(root, document)
        originals[path] = path.read_bytes() if path.exists() else None
        old_doc = original_docs[mid]
        if path.is_file():
            text = path.read_text(encoding='utf-8')
        elif safe_path(root, old_doc).is_file():
            text = relocate_links(root, safe_path(root, old_doc).read_text(encoding='utf-8'), old_doc, document)
        else:
            text = f'# {mid} 项目导览\n'
        neighbors = module.get('children', []) + ([module['parent']] if module.get('parent') else [])
        links = []
        for neighbor in neighbors:
            relative = os.path.relpath(safe_path(root, by_id[neighbor]['document']), path.parent).replace(os.sep, '/')
            links.append(f'- [{neighbor}]({quote(relative, safe="/")})')
        summary = counts(files[p] for p in module.get('files', []) if p in files)
        rows = '\n'.join(f'| {category} | {stats["file_count"]} | {stats["symbol_count"]} |' for category, stats in summary.items())
        review = module.get('review', {})
        scope = review.get('scope', 'unreviewed')
        note = '抽样审阅，未完成整个模块。' if scope == 'sampled' else ('已登记完整审阅范围；仍须运行交付校验。' if scope == 'complete' else '待核实：尚未完成实现阅读。')
        if not module.get('files'):
            note = '本节点不直接归属源码；实现阅读范围见子模块。'
        heading = '模块地图' if mid == output.get('root_module') else '功能点清单'
        content = f'## {heading}\n\n{note}\n\n' + ('\n'.join(links) or '无父子模块导航。')
        content += '\n\n| 源码类别（仅直接归属） | 文件 | 索引符号 |\n| --- | --- | --- |\n' + rows
        text = replace_managed(text, 'draft-navigation', content)
        # Only navigation/statistics are regenerated. Semantic sections are initialized
        # once and stay editable; rerunning the generator must not erase reviewed prose.
        sections = ([('项目定位', 'purpose', '待核实：根据项目入口说明用途与使用者。'),
                     ('架构与数据流', 'architecture', '待核实：根据关键实现补充流程。'),
                     ('构建/运行/测试', 'commands', '待核实：记录命令来源、前提及真实执行状态。')]
                    if mid == output.get('root_module') else
                    [('职责与目录', 'responsibility', '待核实：根据归属实现说明职责与边界。'),
                     ('主要功能', 'behavior', '待核实：根据具体能力说明触发、处理和结果。'),
                     ('依赖与协作', 'collaboration', '待核实：阅读双方调用位置后补充。'),
                     ('测试与验证', 'tests', str(review.get('test', {}).get('status', 'not_reviewed')) + '；' + str(review.get('test', {}).get('reason', '尚未审阅')))])
        for title, key, prompt in sections:
            from completeness import has_section
            if not has_section(text, key):
                text += f'\n## {title}\n\n<!-- agentsmd:placeholder {key} -->\n{prompt}\n<!-- agentsmd:endplaceholder -->\n'
        capabilities = review.get('capabilities') or ([{'id': 'unclassified', 'title': '尚未登记的主要能力', 'aspects': {}}] if module.get('files') else [])
        for capability in capabilities:
            aspects = capability.get('aspects', {})
            for aspect in ASPECTS:
                item = aspects.get(aspect, {})
                if item.get('status') == 'not_applicable':
                    continue
                ids = item.get('claims') or ['draft-' + digest(mid + ':' + capability['id'] + ':' + aspect)[:20]]
                for cid in ids:
                    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', cid) or cid in generated_ids:
                        raise ValueError(f'invalid/duplicate generated claim id: {cid}')
                    generated_ids.add(cid)
                    claim = known.get(cid)
                    if claim and (claim['module'] != mid or claim.get('capability') != capability['id'] or claim.get('aspect') != aspect):
                        raise ValueError(f'claim ownership mismatch: {cid}')
                    if not claim:
                        refs = copy.deepcopy(item['evidence'] if 'evidence' in item else candidate_refs(root, module, files, aspect, capability, candidate_cache))
                        claim = {'id': cid, 'module': mid, 'document': document,
                                 'capability': capability['id'], 'aspect': aspect,
                                 'kind': 'configuration' if aspect == 'configuration' else 'behavior',
                                 'status': 'unknown', 'reason': '自动草稿仅提供候选位置，尚未确认行为与证据的对应关系',
                                 'evidence': refs}
                        claims.append(claim)
                        known[cid] = claim
                    # Preserve any authored claim block verbatim, even when its hash is stale.
                    if re.search(r'<!--\s*agentsmd:claim\s+' + re.escape(cid) + r'\s*-->', text):
                        continue
                    body = f'待核实：{capability["title"]}的{ASPECT_TITLES[aspect]}。以下仅为阅读候选，不表示已确认。'
                    if claim['evidence']:
                        body += '\n\n候选位置：' + '、'.join(citation(root, document, ref) for ref in claim['evidence'])
                    else:
                        body += '\n\n未找到该方面候选证据，需人工定位；这不表示该方面不适用。'
                    text += f'\n<!-- agentsmd:claim {cid} -->\n<!-- agentsmd:draft-claim -->\n{body}\n<!-- agentsmd:endclaim -->\n'
        writes[path] = text
    # Recheck all targets before the first write; reject concurrent user edits.
    validate_targets(root, [m['document'] for m in output['modules']])
    for path, original in originals.items():
        if (path.read_bytes() if path.exists() else None) != original:
            raise ValueError(f'guide changed during generation: {path}; rerun')
    for name in ('modules.draft.json', 'evidence.draft.json'):
        contained_path(root, (destination / name).relative_to(root).as_posix())
    from guidewrites import apply, json_bytes
    batch = {path.relative_to(root).as_posix(): text.encode('utf-8') for path, text in writes.items()}
    before = {path.relative_to(root).as_posix(): value for path, value in originals.items()}
    for name, value in [('modules.draft.json', output), ('evidence.draft.json', {'schema_version': 2, 'root': str(root), 'claims': claims})]:
        path = destination / name
        relative = path.relative_to(root).as_posix()
        before[relative] = dependencies[path] if path in dependencies else (path.read_bytes() if path.exists() else None)
        batch[relative] = json_bytes(value)
    apply(root, batch, before, dependencies=dependencies)
    return output, claims


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', required=True)
    ap.add_argument('--map', dest='mapping')
    ap.add_argument('--ledger')
    ap.add_argument('--report', action='append')
    ap.add_argument('--out-dir', help='preview session directory; default from preview map or .agentsmd/drafts')
    action = ap.add_mutually_exclusive_group()
    action.add_argument('--write', action='store_true', help='update managed blocks at final module document paths')
    action.add_argument('--recover', action='store_true', help='roll back an interrupted guide batch; never overwrite conflicting user edits')
    action.add_argument('--promote', action='store_true', help='publish reviewed preview documents and their map/ledger to final targets')
    action.add_argument('--new-preview', action='store_true', help='create a new session from the final map in an empty --out-dir')
    args = ap.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.recover:
            from guidewrites import recover
            print('Guide batch recovered' if recover(root) else 'No pending guide batch')
            return
        if args.promote:
            from promotion import promote
            map_path = Path(args.mapping) if args.mapping else root / '.agentsmd/drafts/modules.draft.json'
            ledger_path = Path(args.ledger) if args.ledger else root / '.agentsmd/drafts/evidence.draft.json'
            inputs = {path: path.read_bytes() for path in (map_path, ledger_path)}
            final, ledger = promote(root, read_json(map_path), read_json(ledger_path), load_reports(root, args.report), inputs)
            print(f'Promoted {len(final["modules"])} guides and {len(ledger["claims"])} evidence records; review/test/claim statuses unchanged')
            return
        map_path = Path(args.mapping) if args.mapping else root / '.agentsmd/modules.json'
        inputs = {map_path: map_path.read_bytes()}
        mapping = read_json(map_path)
        require(mapping_errors(mapping))
        out_dir = args.out_dir or mapping.get('preview', {}).get('directory', '.agentsmd/drafts')
        ledger_path = Path(args.ledger) if args.ledger else root / '.agentsmd/evidence.json'
        if not args.ledger and 'preview' in mapping:
            ledger_path = root / mapping['preview']['directory'] / 'evidence.draft.json'
        if args.ledger and not args.write and not args.new_preview and 'preview' not in mapping and (root / out_dir / 'modules.draft.json').exists():
            raise ValueError('existing preview owns its ledger; use --map <preview map> when supplying an explicit --ledger')
        if ledger_path.exists(): inputs[ledger_path] = ledger_path.read_bytes()
        ledger = read_json(ledger_path) if ledger_path.exists() else None
        generated, claims = generate(root, mapping, load_reports(root, args.report), ledger, args.write, out_dir, args.new_preview, inputs)
        print(f'{len(generated["modules"])} guide drafts, {len(claims)} evidence records; review before capture')
    except (OSError, ValueError, KeyError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    main()
