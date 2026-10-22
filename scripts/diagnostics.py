"""Lossless structured diagnostics with compact, module-grouped console output."""
from collections import Counter
import json
import re


def add_arguments(parser):
    parser.add_argument('--json', action='store_true', help='all diagnostics and grouped counts as JSON')
    parser.add_argument('--details', action='store_true', help='print every diagnostic instead of compact groups')
    parser.add_argument('--limit', type=int, default=12, help='maximum module groups in compact output')


def code(message):
    lowered = message.lower()
    for token, category in [
        ('expected ', 'schema.invalid'), ('required field', 'schema.invalid'),
        ('must be at least', 'schema.invalid'), ('unknown capability aspect', 'schema.invalid'),
        ('duplicate guide target', 'guide.duplicate'), ('target', 'guide.target'),
        ('review stale', 'review.stale'), ('review snapshot', 'review.stale'),
        ('review source', 'review.stale'), ('unreviewed', 'review.incomplete'),
        ('review scope', 'review.incomplete'), ('reviewed_files', 'review.incomplete'),
        ('every owned source', 'review.incomplete'), ('capability', 'capability.incomplete'),
        ('aspect', 'capability.incomplete'), ('primary behavior', 'evidence.unconfirmed'),
        ('placeholder', 'draft.incomplete'), ('unfinished draft', 'draft.incomplete'),
        ('document section', 'document.incomplete'),
        ('visible citation', 'citation.mismatch'), ('citation', 'citation.mismatch'),
        ('snippet_hash', 'evidence.stale'), ('symbol_hash', 'evidence.stale'),
        ('changed markdown', 'evidence.stale'), ('navigation', 'guide.navigation'),
        ('ownership', 'module.ownership'), ('uncovered source', 'module.ownership'),
        ('syntax index', 'index.incomplete'), ('diagnostics', 'index.incomplete'),
        ('未完整分析', 'index.incomplete'), ('过期', 'index.stale'), ('selection is stale', 'index.stale'),
        ('scope is stale', 'index.stale'), ('test', 'test.record'), ('行号', 'citation.range'),
    ]:
        if token in lowered: return category
    return 'validation.other'


def collect(errors, warnings, mapping=None, ledger=None):
    modules = mapping.get('modules', []) if isinstance(mapping, dict) else []
    modules = [m for m in modules if isinstance(m, dict) and isinstance(m.get('id'), str)] if isinstance(modules, list) else []
    claims = ledger.get('claims', []) if isinstance(ledger, dict) else []
    claims = [c for c in claims if isinstance(c, dict) and isinstance(c.get('id'), str)] if isinstance(claims, list) else []
    claim_modules = {c['id']: c.get('module') for c in claims}
    docs = {m.get('document'): m['id'] for m in modules if isinstance(m.get('document'), str)}
    result, seen = [], set()
    for severity, messages in (('error', errors), ('warning', warnings)):
        for message in messages:
            message = str(message)
            if (severity, message) in seen: continue
            seen.add((severity, message))
            module, field = None, None
            schema = re.search(r'\.(\$.*?): (?:expected|required field|must be at least|unknown capability aspect)', message)
            if schema:
                field = schema.group(1)
                index = re.search(r'\$\.modules\[(\d+)\]', field)
                if index and isinstance(mapping, dict) and isinstance(mapping.get('modules'), list):
                    entries = mapping['modules']; i = int(index.group(1))
                    if i < len(entries) and isinstance(entries[i], dict): module = entries[i].get('id')
                index = re.search(r'\$\.claims\[(\d+)\]', field)
                if index and isinstance(ledger, dict) and isinstance(ledger.get('claims'), list):
                    entries = ledger['claims']; i = int(index.group(1))
                    if i < len(entries) and isinstance(entries[i], dict): module = entries[i].get('module')
            for mid in sorted((m['id'] for m in modules), key=len, reverse=True):
                if message.startswith(mid + ':') or message.startswith(mid + '/'):
                    module = mid; break
            if module is None:
                for cid, mid in claim_modules.items():
                    if message.startswith(cid + ':') or f'claim {cid} ' in message:
                        module = mid; break
            if module is None:
                for doc, mid in docs.items():
                    if message.startswith(doc + ':'):
                        module = mid; break
            # Schema errors can refer to the very field used to group diagnostics.
            if not isinstance(module, str): module = None
            document = next((m.get('document') for m in modules if m['id'] == module), None)
            if not isinstance(document, str): document = None
            result.append({'severity': severity, 'code': code(message), 'module': module,
                           'document': document,
                           'field': field, 'message': message})
    return result


def summary(errors, warnings, mapping=None, ledger=None):
    entries = collect(errors, warnings, mapping, ledger)
    groups = {}
    for entry in entries:
        name = entry['module'] or '(project)'
        group = groups.setdefault(name, {'module': entry['module'], 'errors': 0, 'warnings': 0, 'codes': Counter()})
        group['errors' if entry['severity'] == 'error' else 'warnings'] += 1
        group['codes'][entry['code']] += 1
    return {'status': 'failed' if errors else 'ok',
            'error_count': sum(e['severity'] == 'error' for e in entries),
            'warning_count': sum(e['severity'] == 'warning' for e in entries),
            'groups': [{**g, 'codes': dict(sorted(g['codes'].items()))} for _, g in sorted(groups.items())],
            'diagnostics': entries}


def render(errors, warnings, mapping=None, ledger=None, options=None, metadata=None):
    result = summary(errors, warnings, mapping, ledger)
    if getattr(options, 'strict', False) and warnings: result['status'] = 'failed'
    if metadata: result.update(metadata)
    if getattr(options, 'json', False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    entries = result['diagnostics']
    if getattr(options, 'details', False):
        for entry in entries: print(f"  {entry['severity'].upper()} [{entry['code']}] {entry['message']}")
    else:
        limit = max(1, getattr(options, 'limit', 12))
        ordered = sorted(result['groups'], key=lambda g: (-g['errors'], -g['warnings'], g['module'] or ''))
        for group in ordered[:limit]:
            codes = ', '.join(f'{key}={count}' for key, count in group['codes'].items())
            print(f"  {group['module'] or '(project)'}: {group['errors']} errors, {group['warnings']} warnings; {codes}")
            # One example per severity/code retains both failures and reading hints.
            examples = set()
            for entry in entries:
                if entry['module'] != group['module']: continue
                key = entry['severity'], entry['code']
                if key not in examples:
                    print(f"    {entry['severity'].upper()} {entry['message']}")
                    examples.add(key)
        if len(ordered) > limit:
            print(f'  … {len(ordered) - limit} more module groups; use --details or --json for all diagnostics')
    return result
