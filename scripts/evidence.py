#!/usr/bin/env python3
"""Bind important Markdown claims to versioned source ranges and symbol identities."""
import argparse
import copy
import re
from pathlib import Path
from artifacts import read_json, write_json, safe_path, file_map, load_reports, ledger_path as paired_ledger
from scanpolicy import digest
from citations import validate_citations

BLOCK = re.compile(r'<!--\s*agentsmd:claim\s+([A-Za-z0-9_.:-]+)\s*-->(.*?)<!--\s*agentsmd:endclaim\s*-->', re.S)
ROLES = {'entrypoint', 'implementation', 'caller', 'callee', 'configuration', 'interface', 'test'}
KINDS = {'behavior', 'flow', 'configuration', 'interface', 'test'}


def blocks(root, document):
    return parse_blocks(safe_path(root, document).read_text(encoding='utf-8'), document)


def parse_blocks(text, document):
    from verify import without_fences
    text = without_fences(text)
    found = {}
    for match in BLOCK.finditer(text):
        cid, statement = match.groups()
        if cid in found or 'agentsmd:claim' in statement:
            raise ValueError(f'{document}: duplicate/nested claim block {cid}')
        found[cid] = statement.strip()
    if len(found) != len(re.findall(r'<!--\s*agentsmd:claim\s+', text)) or len(found) != len(re.findall(r'<!--\s*agentsmd:endclaim\s*-->', text)):
        raise ValueError(f'{document}: unbalanced claim markers')
    return found


def resolve_ref(root, ref, files):
    path = ref['path']
    raw = safe_path(root, path).read_bytes()
    lines = raw.splitlines(keepends=True)
    start, end = ref['line'], ref.get('end_line', ref['line'])
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
        raise ValueError(f'{path}: invalid evidence range {start}..{end}')
    result = {'file_hash': digest(raw), 'snippet_hash': digest(b''.join(lines[start - 1:end]))}
    if files.get(path, {}).get('content_hash') and files[path]['content_hash'] != digest(raw):
        raise ValueError(f'{path}: index stale; reanalyze before capture/check')
    if ref.get('symbol_id'):
        hits = [s for s in files.get(path, {}).get('symbols') or [] if s.get('id') == ref['symbol_id']]
        if len(hits) != 1:
            raise ValueError(f"{path}: unknown/ambiguous symbol {ref['symbol_id']}")
        symbol = hits[0]
        if not symbol['line'] <= start <= end <= symbol['end_line']:
            raise ValueError(f'{path}: evidence range is outside symbol')
        if not symbol.get('content_hash'):
            raise ValueError(f'{path}: symbol has no syntax fingerprint; use a source range')
        result['symbol_hash'] = symbol['content_hash']
    return result


def capture(root, ledger, reports):
    from schemas import ledger_errors, require
    require(ledger_errors(ledger, draft=True))
    captured = copy.deepcopy(ledger)
    files = file_map(reports)
    cache = {}
    for claim in captured.get('claims', []):
        document = claim['document']
        if document not in cache: cache[document] = blocks(root, document)
        if claim['id'] not in cache[document]:
            raise ValueError(f"{document}: missing claim block {claim['id']}")
        claim['statement'] = cache[document][claim['id']]
        claim['statement_hash'] = digest(claim['statement'])
        for ref in claim.get('evidence', []):
            ref.update(resolve_ref(root, ref, files))
    captured['schema_version'] = 1
    captured['root'] = str(Path(root).resolve())
    return captured


def check(root, ledger, mapping, reports, require_modules=False):
    from schemas import mapping_errors, ledger_errors
    errors, warnings, cache = mapping_errors(mapping) + ledger_errors(ledger), [], {}
    if errors: return errors, warnings
    root = Path(root).resolve()
    if Path(ledger.get('root', '')).resolve() != root:
        errors.append('evidence ledger root mismatch')
    modules = {m['id']: m for m in mapping['modules']}
    files = file_map(reports)
    documents = {}
    def document_blocks(document):
        if document not in cache:
            documents[document] = safe_path(root, document).read_text(encoding='utf-8')
            cache[document] = parse_blocks(documents[document], document)
        return cache[document]
    seen, covered, claim_documents = set(), set(), {}
    for claim in ledger.get('claims', []):
        cid = claim.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', cid) or cid in seen:
            errors.append(f'{cid}: invalid/duplicate claim id')
        seen.add(cid)
        claim_documents[cid] = claim.get('document')
        module = modules.get(claim.get('module'))
        if not module or claim.get('document') != module['document']:
            errors.append(f'{cid}: module/document mismatch')
        kind, status = claim.get('kind'), claim.get('status')
        if kind not in KINDS or status not in ('confirmed', 'inferred', 'unknown'):
            errors.append(f'{cid}: invalid kind/status')
        if status in ('inferred', 'unknown') and not claim.get('reason'):
            errors.append(f'{cid}: inference/uncertainty needs a reason')
        try:
            document = claim['document']
            statement = document_blocks(document).get(cid)
            if statement and status == 'confirmed':
                from completeness import unfinished
                errors.extend(f'{cid}: {message}' for message in unfinished(statement))
            if statement:
                errors.extend(f'{cid}: {error}' for error in validate_citations(root, document, statement, claim.get('evidence', []), documents[document]))
            if not statement or claim.get('statement') != statement or claim.get('statement_hash') != digest(statement):
                errors.append(f'{cid}: missing or changed Markdown claim; review then recapture')
            if status == 'inferred' and statement and not re.search(r'推断|推测|inferred', statement, re.I):
                errors.append(f'{cid}: inferred status must be visible in prose')
            if status == 'unknown' and statement and not re.search(r'待核实|未确认|unknown|unconfirmed', statement, re.I):
                errors.append(f'{cid}: unknown status must be visible in prose')
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f'{cid}: {exc}')
        refs = claim.get('evidence', [])
        roles = {ref.get('role') for ref in refs}
        if not roles <= ROLES:
            errors.append(f'{cid}: invalid evidence role')
        required = {'behavior': {'implementation'}, 'flow': {'caller', 'callee'},
                    'configuration': {'configuration', 'implementation'},
                    'interface': {'interface'}, 'test': {'test'}}.get(kind, set())
        if status == 'confirmed' and not required <= roles:
            errors.append(f"{cid}: confirmed {kind} needs roles {', '.join(sorted(required))}")
        if status == 'confirmed' and module:
            owned = set(module.get('files', []))
            primary_roles = {'behavior': {'implementation'}, 'flow': {'callee'}, 'interface': {'interface'}}.get(kind, set())
            # Declaration-only and test-only modules should not invent a runtime workflow.
            if kind == 'test' and owned and all(files.get(p, {}).get('is_test') for p in owned):
                primary_roles = {'test'}
            if primary_roles:
                if not any(r.get('path') in owned and r.get('role') in primary_roles for r in refs):
                    errors.append(f'{cid}: primary evidence must be owned by module')
                else:
                    covered.add(claim.get('module'))
        for ref in refs:
            try:
                current = resolve_ref(root, ref, files)
                # Unrelated edits outside this range are allowed; whole-file hash is retained for audit.
                for key in ('snippet_hash', 'symbol_hash'):
                    if key in current and current[key] != ref.get(key):
                        errors.append(f"{cid}: stale {key}: {ref['path']}; review evidence")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f'{cid}: {exc}')
    by_claim = {c.get('id'): c for c in ledger.get('claims', [])}
    for module in modules.values():
        review = module.get('review', {})
        if require_modules or review.get('scope') == 'complete':
            from reviewstate import freshness
            errors.extend(freshness(root, module, files))
            from modules import check_capabilities
            if module.get('files'):
                errors.extend(check_capabilities(module['id'], review.get('capabilities', [])))
            for capability in review.get('capabilities', []):
                cap_id = capability['id']
                primary = capability.get('claims', [])
                aspects = capability.get('aspects', {})
                for aspect, item in aspects.items():
                    if item.get('status') == 'covered':
                        for claim_id in item.get('claims', []):
                            claim = by_claim.get(claim_id, {})
                            if claim.get('aspect') != aspect:
                                errors.append(f'{module["id"]}/{cap_id}: {claim_id} does not prove aspect {aspect}')
                            primary = primary + [claim_id]
                primary_claims = [by_claim.get(cid, {}) for cid in capability.get('claims', [])]
                if any(c.get('kind') in ('behavior', 'flow', 'test') for c in primary_claims) and aspects.get('success', {}).get('status') != 'covered':
                    errors.append(f'{module["id"]}/{cap_id}: executable capability requires a covered success result')
                for claim_id in set(primary):
                    claim = by_claim.get(claim_id, {})
                    if claim.get('module') != module['id'] or claim.get('capability') != cap_id or claim.get('status') != 'confirmed':
                        errors.append(f'{module["id"]}/{cap_id}: missing/unconfirmed capability claim {claim_id}')
                    if claim.get('aspect') == 'configuration' and claim.get('kind') != 'configuration':
                        errors.append(f'{claim_id}: configuration aspect needs configuration source and implementation evidence')
        document = module['document']
        try:
            for cid in document_blocks(document):
                if claim_documents.get(cid) != document: errors.append(f'{document}: claim {cid} missing from ledger or duplicated across documents')
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
        if module.get('files') and module['id'] not in covered:
            (errors if require_modules else warnings).append(f"{module['id']}: no confirmed primary behavior/interface/test claim")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['capture', 'check'])
    ap.add_argument('--root', default='.')
    ap.add_argument('--ledger', help='default .agentsmd/evidence.json')
    ap.add_argument('--map', dest='mapping', help='default .agentsmd/modules.json')
    ap.add_argument('--draft', help='capture input; default existing ledger')
    ap.add_argument('--report', action='append')
    ap.add_argument('--require-modules', action='store_true')
    from diagnostics import add_arguments
    add_arguments(ap)
    args = ap.parse_args()
    root = Path(args.root).resolve()
    map_path = Path(args.mapping) if args.mapping else root / '.agentsmd/modules.json'
    try:
        reports = load_reports(root, args.report)
        mapping = read_json(map_path)
        from schemas import mapping_errors, require
        require(mapping_errors(mapping))
        ledger_path = paired_ledger(root, mapping, args.ledger)
        ledger = read_json(args.draft or ledger_path)
        if args.command == 'capture':
            ledger = capture(root, ledger, reports)
        errors, warnings = check(root, ledger, mapping, reports, args.require_modules)
        from diagnostics import render
        render(errors, warnings, mapping, ledger, args)
        if errors: raise SystemExit(1)
        if args.command == 'capture': write_json(ledger_path, ledger)
    except (OSError, ValueError, KeyError) as exc:
        from diagnostics import render
        render(getattr(exc, 'errors', [str(exc)]), [], options=args)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
