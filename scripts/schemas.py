"""Small standard-library validators for editable analysis artifacts.

Validate shape before traversing data. Semantic consistency belongs to the
module/evidence checkers; unknown extension fields remain forward compatible.
"""
import re
from pathlib import PurePosixPath


class SchemaError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('\n'.join(errors))


class Validator:
    def __init__(self, name):
        self.name, self.errors = name, []

    def error(self, path, message):
        self.errors.append(f'{self.name}.{path}: {message}')

    def obj(self, value, path):
        if not isinstance(value, dict):
            self.error(path, 'expected object')
            return False
        return True

    def array(self, value, path):
        if not isinstance(value, list):
            self.error(path, 'expected array')
            return False
        return True

    def string(self, value, path, empty=False):
        if not isinstance(value, str) or (not empty and not value.strip()):
            self.error(path, 'expected non-empty string' if not empty else 'expected string')
            return False
        return True

    def field(self, item, name, path, check, required=False):
        if name in item:
            check(item[name], path + '.' + name)
        elif required:
            self.error(path + '.' + name, 'required field missing')

    def strings(self, value, path):
        if self.array(value, path):
            for i, item in enumerate(value): self.string(item, f'{path}[{i}]')

    def boolean(self, value, path):
        if type(value) is not bool: self.error(path, 'expected boolean')

    def integer(self, value, path):
        if type(value) is not int: self.error(path, 'expected integer')

    def relative(self, value, path):
        if self.string(value, path):
            p = PurePosixPath(value)
            if '\x00' in value or '\\' in value or p.is_absolute() or '..' in p.parts or str(p) != value or value == '.':
                self.error(path, 'expected normalized project-relative file path')

    def paths(self, value, path):
        if self.array(value, path):
            for i, item in enumerate(value): self.relative(item, f'{path}[{i}]')

    def hashes(self, value, path):
        if self.obj(value, path):
            for name, hashed in value.items():
                self.relative(name, path + f'[{name!r}]')
                if not isinstance(hashed, str) or not re.fullmatch(r'[0-9a-f]{64}', hashed):
                    self.error(path + f'[{name!r}]', 'expected SHA-256 hex digest')

    def nullable_hash(self, value, path):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)):
            self.error(path, 'expected SHA-256 hex digest or null')

    def finish(self):
        return self.errors


def scope_fields(v, scope, path):
    if not isinstance(scope, dict): return
    for name in ('selection_hash',): v.field(scope, name, path, v.string)
    v.field(scope, 'gitignore_applied', path, v.boolean)
    if 'options' in scope and v.obj(scope['options'], path + '.options'):
        options = scope['options']
        v.field(options, 'exclude', path + '.options', v.strings, True)
        if 'respect_gitignore' not in options:
            v.error(path + '.options.respect_gitignore', 'required field missing')
        elif options['respect_gitignore'] is not None:
            v.boolean(options['respect_gitignore'], path + '.options.respect_gitignore')
    if 'policy' in scope and v.obj(scope['policy'], path + '.policy'):
        policy = scope['policy']
        for name in ('exclude', 'include_hidden'): v.field(policy, name, path + '.policy', v.strings)
        v.field(policy, 'respect_gitignore', path + '.policy', v.boolean)


def reproducible_scope_errors(scope, name='scope'):
    v = Validator(name)
    if not v.obj(scope, '$'): return v.errors
    scope_fields(v, scope, '$')
    for key in ('selection_hash', 'options', 'policy', 'gitignore_applied'):
        if key not in scope: v.error('$.' + key, 'required for reproducible selection; rerun analyze.py')
    value = scope.get('selection_hash')
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)):
        v.error('$.selection_hash', 'expected SHA-256 hex digest; rerun analyze.py')
    policy = scope.get('policy')
    if isinstance(policy, dict):
        for key in ('exclude', 'include_hidden', 'respect_gitignore'):
            if key not in policy: v.error('$.policy.' + key, 'required field missing')
    return v.errors


def selection_errors(selection):
    v = Validator('selection')
    if not v.obj(selection, '$'): return v.errors
    v.field(selection, 'root', '$', v.string, True)
    v.field(selection, 'files', '$', v.paths, True)
    files = selection.get('files')
    if isinstance(files, list) and all(isinstance(p, str) for p in files) and len(set(files)) != len(files):
        v.error('$.files', 'duplicate selection paths')
    v.errors.extend(reproducible_scope_errors(selection.get('scope'), 'selection.scope'))
    return v.errors


def snapshot_errors(value, name='snapshot', current=False, bundle=False):
    v = Validator(name)
    if not v.obj(value, '$'): return v.errors
    sid = value.get('snapshot_id')
    if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-f]{32}', sid):
        v.error('$.snapshot_id', 'expected generation UUID hex digest')
    allowed = {'selection.json', 'context.json', 'index.json', 'ast.json', 'module-candidates.json'}
    for key in (('hashes',) if bundle else ('hashes', 'artifacts')):
        if not v.obj(value.get(key), '$.' + key): continue
        for artifact, content in value[key].items():
            if artifact not in allowed: v.error('$.' + key, 'unsupported analysis artifact: ' + str(artifact))
            if key == 'hashes':
                if not isinstance(content, str) or not re.fullmatch(r'[0-9a-f]{64}', content):
                    v.error('$.hashes.' + artifact, 'expected SHA-256 hex digest')
            elif content != f'snapshots/{sid}/{artifact}':
                v.error('$.artifacts.' + artifact, 'expected path within the selected generation')
    if not bundle and isinstance(value.get('hashes'), dict) and isinstance(value.get('artifacts'), dict):
        if value['hashes'].keys() != value['artifacts'].keys():
            v.error('$.hashes', 'expected exactly one checksum for each artifact')
    if current:
        if value.get('agentsmd_generation') is not True: v.error('$.agentsmd_generation', 'expected true')
        if value.get('previous') is not None:
            v.errors.extend(snapshot_errors(value['previous'], name + '.previous'))
    return v.errors


def ref(v, item, path):
    if not v.obj(item, path): return
    for name, check in [('role', v.string), ('path', v.relative), ('line', v.integer)]:
        v.field(item, name, path, check, True)
    v.field(item, 'end_line', path, v.integer)
    for name in ('symbol_id', 'file_hash', 'snippet_hash', 'symbol_hash'):
        v.field(item, name, path, v.string)
    for name in ('line', 'end_line'):
        if type(item.get(name)) is int and item[name] < 1:
            v.error(path + '.' + name, 'must be at least 1')


def refs(v, items, path):
    if v.array(items, path):
        for i, item in enumerate(items): ref(v, item, f'{path}[{i}]')


def capability(v, item, path):
    if not v.obj(item, path): return
    for name in ('id', 'title', 'status'):
        v.field(item, name, path, v.string, name == 'id')
    v.field(item, 'claims', path, v.strings)
    if 'aspects' in item and v.obj(item['aspects'], path + '.aspects'):
        for name, aspect in item['aspects'].items():
            ap = path + '.aspects.' + name
            if name not in ('input', 'success', 'failure', 'configuration'):
                v.error(ap, 'unknown capability aspect')
            if not v.obj(aspect, ap): continue
            v.field(aspect, 'status', ap, v.string, True)
            v.field(aspect, 'reason', ap, lambda a, p: v.string(a, p, True))
            v.field(aspect, 'claims', ap, v.strings)
            if 'evidence' in aspect: refs(v, aspect['evidence'], ap + '.evidence')


def mapping_errors(mapping):
    v = Validator('modules')
    if not v.obj(mapping, '$'): return v.errors
    for name, check in [('root', v.string), ('root_module', v.string), ('modules', v.array)]:
        v.field(mapping, name, '$', check, True)
    for name in ('schema_version', 'source_count'): v.field(mapping, name, '$', v.integer)
    for name in ('scope', 'delivery', 'budgets'): v.field(mapping, name, '$', v.obj)
    scope_fields(v, mapping.get('scope'), '$.scope')
    if 'preview' in mapping and v.obj(mapping['preview'], '$.preview'):
        preview = mapping['preview']
        v.field(preview, 'directory', '$.preview', v.relative, True)
        v.field(preview, 'artifact_hashes', '$.preview', v.obj, True)
        if isinstance(preview.get('artifact_hashes'), dict):
            for name in ('modules.json', 'evidence.json'):
                v.field(preview['artifact_hashes'], name, '$.preview.artifact_hashes', v.nullable_hash, True)
    delivery = mapping.get('delivery')
    if isinstance(delivery, dict):
        v.field(delivery, 'mode', '$.delivery', v.string)
        v.field(delivery, 'scope_confirmed', '$.delivery', v.boolean)
    for i, module in enumerate(mapping.get('modules', []) if isinstance(mapping.get('modules'), list) else []):
        path = f'$.modules[{i}]'
        if not v.obj(module, path): continue
        for name, check in [('id', v.string), ('document', v.relative), ('files', v.paths)]:
            v.field(module, name, path, check, True)
        if 'parent' in module and module['parent'] is not None: v.string(module['parent'], path + '.parent')
        v.field(module, 'children', path, v.strings)
        if 'final_document' in module or 'final_document_hash' in module:
            v.field(module, 'final_document', path, v.relative, True)
            v.field(module, 'final_document_hash', path, v.nullable_hash, True)
        for name in ('oversized', 'needs_manual_split'): v.field(module, name, path, v.boolean)
        if 'review' not in module or not v.obj(module['review'], path + '.review'): continue
        review, rp = module['review'], path + '.review'
        v.field(review, 'scope', rp, v.string)
        v.field(review, 'recorded_at', rp, v.string)
        for name in ('entrypoint', 'implementation', 'reviewed_files'): v.field(review, name, rp, v.paths)
        v.field(review, 'source_hashes', rp, v.hashes)
        if 'exclusions' in review and v.array(review['exclusions'], rp + '.exclusions'):
            for j, item in enumerate(review['exclusions']):
                ep = rp + f'.exclusions[{j}]'
                if not v.obj(item, ep): continue
                v.field(item, 'path', ep, v.relative, True)
                v.field(item, 'reason', ep, lambda a, p: v.string(a, p, True), True)
        if 'capabilities' in review and v.array(review['capabilities'], rp + '.capabilities'):
            for j, item in enumerate(review['capabilities']): capability(v, item, rp + f'.capabilities[{j}]')
        if 'test' in review and v.obj(review['test'], rp + '.test'):
            test, tp = review['test'], rp + '.test'
            v.field(test, 'status', tp, v.string)
            v.field(test, 'reason', tp, lambda a, p: v.string(a, p, True))
            if 'execution' in test and v.obj(test['execution'], tp + '.execution'):
                for name in ('command', 'cwd', 'timestamp'): v.field(test['execution'], name, tp + '.execution', v.string)
                v.field(test['execution'], 'exit_code', tp + '.execution', v.integer)
                v.field(test['execution'], 'source_hashes', tp + '.execution', v.hashes)
                v.field(test['execution'], 'argv', tp + '.execution', v.strings)
                v.field(test['execution'], 'inputs_unchanged', tp + '.execution', v.boolean)
                v.field(test['execution'], 'scope', tp + '.execution', v.obj)
                scope_fields(v, test['execution'].get('scope'), tp + '.execution.scope')
    return v.finish()


def ledger_errors(ledger, draft=False):
    v = Validator('evidence')
    if not v.obj(ledger, '$'): return v.errors
    v.field(ledger, 'claims', '$', v.array, True)
    v.field(ledger, 'root', '$', v.string, not draft)
    v.field(ledger, 'schema_version', '$', v.integer)
    for i, claim in enumerate(ledger.get('claims', []) if isinstance(ledger.get('claims'), list) else []):
        path = f'$.claims[{i}]'
        if not v.obj(claim, path): continue
        for name in ('id', 'module', 'kind', 'status'): v.field(claim, name, path, v.string, True)
        v.field(claim, 'document', path, v.relative, True)
        for name in ('reason', 'statement'): v.field(claim, name, path, lambda a, p: v.string(a, p, True))
        for name in ('capability', 'aspect', 'statement_hash'): v.field(claim, name, path, v.string)
        if 'evidence' not in claim: v.error(path + '.evidence', 'required field missing')
        else: refs(v, claim['evidence'], path + '.evidence')
    return v.finish()


def report_errors(report, name='report'):
    v = Validator(name)
    if not v.obj(report, '$'): return v.errors
    v.field(report, 'root', '$', v.string, True)
    v.field(report, 'files', '$', v.array, True)
    for key in ('scope', 'stats'): v.field(report, key, '$', v.obj)
    scope_fields(v, report.get('scope'), '$.scope')
    for key in ('snapshot_id', 'analysis_revision'): v.field(report, key, '$', v.string)
    for key in ('notices',): v.field(report, key, '$', v.strings)
    v.field(report, 'diagnostics', '$', v.array)
    for i, file in enumerate(report.get('files', []) if isinstance(report.get('files'), list) else []):
        fp = f'$.files[{i}]'
        if not v.obj(file, fp): continue
        v.field(file, 'path', fp, v.relative, True)
        for key in ('line_end', 'size_bytes'): v.field(file, key, fp, v.integer)
        v.field(file, 'content_hash', fp, v.string)
        v.field(file, 'categories', fp, v.strings)
        if file.get('imports') is not None: v.field(file, 'imports', fp, v.strings)
        for name in ('is_test', 'generated'): v.field(file, name, fp, v.boolean)
        v.field(file, 'candidate_syntax_valid', fp, v.boolean)
        for key in ('jsx_ranges', 'candidate_exclusions'):
            if key not in file or not v.array(file[key], fp + '.' + key): continue
            last = 0
            for j, span in enumerate(file[key]):
                jp = fp + f'.{key}[{j}]'
                if not v.obj(span, jp): continue
                for name in ('start', 'end'): v.field(span, name, jp, v.integer, True)
                if type(span.get('start')) is int and type(span.get('end')) is int:
                    if not 0 <= last <= span['start'] < span['end']:
                        v.error(jp, 'expected ordered non-overlapping UTF-16 offsets')
                    last = span['end']
                if key == 'candidate_exclusions': v.field(span, 'literal', jp, v.boolean, True)
        if 'category' in file and (not isinstance(file['category'], str) or file['category'] not in ('production', 'test', 'generated')):
            v.error(fp + '.category', 'expected production/test/generated')
        if file.get('symbols') is not None and 'symbols' in file and v.array(file['symbols'], fp + '.symbols'):
            for j, symbol in enumerate(file['symbols']):
                sp = fp + f'.symbols[{j}]'
                if not v.obj(symbol, sp): continue
                for name in ('name', 'kind'): v.field(symbol, name, sp, v.string, True)
                for name in ('line', 'end_line'): v.field(symbol, name, sp, v.integer, True)
                for name in ('id', 'content_hash', 'signature_hash', 'body_hash'):
                    v.field(symbol, name, sp, v.string)
                if symbol.get('fields') is not None and v.array(symbol['fields'], sp + '.fields'):
                    for k, field in enumerate(symbol['fields']):
                        field_path = sp + f'.fields[{k}]'
                        if not v.obj(field, field_path): continue
                        v.field(field, 'name', field_path, v.string, True)
                        v.field(field, 'line', field_path, v.integer, True)
    return v.finish()


def require(errors):
    if errors: raise SchemaError(errors)
