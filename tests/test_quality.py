"""Regression tests for enforced citations, delivery scope, drafts and generations."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifacts
import callgraph
import citations
import codeindex
import diff
import draft
import evidence
import modules
import projectcontext
import snapshots

TYPESCRIPT = os.environ.get('AGENTSMD_TYPESCRIPT')


class Quality(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def reports(self):
        return [projectcontext.scan(self.root), codeindex.scan(self.root, js_parser='regex')]

    def fixture(self):
        self.write('src/service.py', 'def run(value):\n    if value < 0: raise ValueError("negative")\n    return value + 1\n')
        reports = self.reports()
        mapping = modules.plan(self.root, reports)
        module = next(m for m in mapping['modules'] if m['files'])
        document = module['document']
        ref = {'path': 'src/service.py', 'line': 1, 'end_line': 3, 'role': 'implementation'}
        body = 'run 对合法输入返回输入加一。\n' + citations.citation(self.root, document, ref)
        self.write(document, '<!-- agentsmd:claim run-success -->\n' + body + '\n<!-- agentsmd:endclaim -->\n')
        self.write('AGENTS.md', f'[src]({document})\n')
        with (self.root / document).open('a') as stream:
            stream.write('\n[Parent](AGENTS.md)\n')
        ledger = evidence.capture(self.root, {'claims': [{'id': 'run-success', 'module': module['id'],
            'document': document, 'kind': 'behavior', 'status': 'confirmed', 'capability': 'run',
            'aspect': 'success', 'evidence': [ref]}]}, reports)
        return reports, mapping, module, ledger

    def complete(self, mapping, module):
        mapping['delivery'] = {'mode': 'complete', 'scope_confirmed': True}
        module['review'].update(scope='complete', reviewed_files=list(module['files']),
            entrypoint=list(module['files']), implementation=list(module['files']),
            test={'status': 'not_run', 'reason': 'Fixture not executed'}, capabilities=[{
                'id': 'run', 'title': 'Run', 'status': 'reviewed', 'claims': ['run-success'],
                'aspects': {aspect: ({'status': 'covered', 'claims': ['run-success']} if aspect == 'success'
                    else {'status': 'not_applicable', 'reason': 'Explicit fixture scope limitation'}) for aspect in modules.ASPECTS}}])
        from reviewstate import record
        stamped = record(self.root, mapping, self.reports(), [module['id']])
        module['review'].update(next(m for m in stamped['modules'] if m['id'] == module['id'])['review'])
        with (self.root / 'AGENTS.md').open('a') as stream:
            stream.write('\n## 项目定位\n用于演示参数计算的独立样例。\n## 架构与数据流\n入口检查参数后返回计算结果。\n## 构建/运行/测试\n无需构建或服务；本样例测试未运行。\n')
        with (self.root / module['document']).open('a') as stream:
            stream.write('\n## 职责与目录\nsrc/service.py 负责参数检查和结果计算。\n## 主要功能\nrun 对合法输入计算结果，负值触发异常。\n## 依赖与协作\n不调用外部服务，使用 Python 内置算术。\n## 测试与验证\n本次未运行目标测试，仅核对源码。\n')

    def test_visible_citations_must_match_ledger_paths_and_ranges(self):
        reports, mapping, module, ledger = self.fixture()
        self.assertEqual(evidence.check(self.root, ledger, mapping, reports)[0], [])
        for visible, expected in [('', 'missing visible'),
                ('[service](src/service.py)', 'path and line'),
                ('[src/service.py:L1](src/service.py#L1)', 'missing visible'),
                ('[src/service.py:L1-L3](src/service.py)', 'anchor missing')]:
            with self.subTest(visible=visible):
                self.write(module['document'], '<!-- agentsmd:claim run-success -->\nrun 返回结果。\n' + visible + '\n<!-- agentsmd:endclaim -->')
                recaptured = evidence.capture(self.root, ledger, reports)
                errors, _ = evidence.check(self.root, recaptured, mapping, reports)
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_nested_document_links_handle_spaces_and_reject_wrong_targets(self):
        self.write('src/my file.py', 'value=1\n')
        self.write('src/other.py', 'value=2\n')
        ref = {'path': 'src/my file.py', 'line': 1}
        label = citations.citation(self.root, 'docs/AGENTS.sample.md', ref)
        self.assertIn('../src/my%20file.py#L1', label)
        self.assertEqual(citations.validate_citations(self.root, 'docs/AGENTS.sample.md', label, [ref]), [])
        errors = citations.validate_citations(self.root, 'docs/AGENTS.sample.md', label.replace('my%20file.py', 'other.py'), [ref])
        self.assertTrue(any('label/target mismatch' in e for e in errors))

    def test_sampled_and_legacy_reviews_cannot_pass_complete_gate(self):
        reports, mapping, module, _ = self.fixture()
        module['review'].update(scope='sampled', entrypoint=module['files'], implementation=module['files'],
                               test={'status': 'not_run', 'reason': 'Not executed'})
        errors, _ = modules.check(self.root, mapping, reports, documents=False, require_reviewed=True)
        self.assertTrue(any('scope is sampled' in e for e in errors))
        self.assertTrue(any('capability checklist' in e for e in errors))
        self.assertTrue(any('every owned source' in e for e in errors))
        errors, _ = modules.check(self.root, mapping, reports)
        self.assertTrue(any('sampled review must be visible' in e for e in errors))

    def test_complete_review_accounts_for_every_source_and_major_capability(self):
        reports, mapping, module, ledger = self.fixture()
        self.complete(mapping, module)
        self.assertEqual(modules.check(self.root, mapping, reports, require_reviewed=True)[0], [])
        self.assertEqual(evidence.check(self.root, ledger, mapping, reports, True)[0], [])
        module['review']['reviewed_files'] = []
        module['review']['exclusions'] = [{'path': 'src/service.py', 'reason': ''}]
        errors, _ = modules.check(self.root, mapping, reports, require_reviewed=True)
        self.assertTrue(any('exclusions require reasons' in e for e in errors))
        self.assertTrue(any('must be in reviewed_files' in e for e in errors))

    def test_aspects_require_distinct_matching_confirmed_claims(self):
        reports, mapping, module, ledger = self.fixture()
        self.complete(mapping, module)
        cap = module['review']['capabilities'][0]
        cap['aspects']['failure'] = {'status': 'covered', 'claims': ['run-success']}
        errors, _ = evidence.check(self.root, ledger, mapping, reports, True)
        self.assertTrue(any('does not prove aspect failure' in e for e in errors))
        cap['aspects']['configuration'] = {'status': 'unknown', 'reason': 'Not read'}
        errors, _ = modules.check(self.root, mapping, reports, require_reviewed=True)
        self.assertTrue(any('unresolved capability aspect' in e for e in errors))
        cap['claims'] = ['missing']
        errors, _ = evidence.check(self.root, ledger, mapping, reports, True)
        self.assertTrue(any('unconfirmed capability claim missing' in e for e in errors))

    def test_configuration_aspect_needs_configuration_and_reading_evidence(self):
        reports, mapping, module, ledger = self.fixture()
        self.complete(mapping, module)
        ledger['claims'][0]['aspect'] = 'configuration'
        cap = module['review']['capabilities'][0]
        cap['aspects']['configuration'] = {'status': 'covered', 'claims': ['run-success']}
        errors, _ = evidence.check(self.root, ledger, mapping, reports, True)
        self.assertTrue(any('configuration source and implementation' in e for e in errors))

    def test_drafts_are_linked_unconfirmed_and_preserve_authored_content(self):
        reports, mapping, module, _ = self.fixture()
        original = (self.root / module['document']).read_text()
        module['review']['capabilities'] = [{'id': 'run', 'title': 'Run', 'aspects': {}}]
        output, claims = draft.generate(self.root, mapping, reports)
        self.assertEqual(len(claims), 4)
        self.assertTrue(all(c['status'] == 'unknown' for c in claims))
        repeated, repeated_claims = draft.generate(self.root, output, reports, {'claims': claims})
        self.assertEqual([m['document'] for m in output['modules']], [m['document'] for m in repeated['modules']])
        self.assertEqual({c['id'] for c in claims}, {c['id'] for c in repeated_claims})
        self.assertEqual((self.root / module['document']).read_text(), original)
        preview = next(m for m in output['modules'] if m['id'] == module['id'])
        text = (self.root / preview['document']).read_text()
        self.assertIn('待核实', text)
        for claim in claims:
            body = evidence.blocks(self.root, claim['document'])[claim['id']]
            self.assertEqual(citations.validate_citations(self.root, claim['document'], body, claim['evidence']), [])
        # Final writing is guarded by the module-boundary review, not an extra approval flow.
        with self.assertRaises(ValueError): draft.generate(self.root, mapping, reports, write=True)
        mapping['delivery']['scope_confirmed'] = True
        draft.generate(self.root, mapping, reports, write=True)
        self.assertTrue((self.root / module['document']).read_text().startswith(original.rstrip()))
        text = (self.root / module['document']).read_text().replace('run 对合法输入返回输入加一。', '人工维护的描述。')
        self.write(module['document'], text)
        draft.generate(self.root, mapping, reports, write=True)
        rewritten = (self.root / module['document']).read_text()
        self.assertIn('人工维护的描述。', rewritten)
        self.assertEqual(rewritten.count('<!-- agentsmd:draft-navigation -->'), 1)

    def test_draft_rejects_duplicate_managed_blocks_before_writing(self):
        reports, mapping, module, _ = self.fixture()
        mapping['delivery']['scope_confirmed'] = True
        self.write(module['document'], '<!-- agentsmd:draft-navigation -->unterminated')
        with self.assertRaises(ValueError): draft.generate(self.root, mapping, reports, write=True)

    def values(self, value):
        report = {'root': str(self.root), 'scope': {'selection_hash': 'same'}, 'files': [], 'value': value}
        return {'context.json': copy.deepcopy(report), 'index.json': copy.deepcopy(report)}

    def test_snapshot_publish_is_atomic_and_previous_generation_is_readable(self):
        out = self.root / '.agentsmd'
        first = snapshots.publish(out, self.values(1))
        def fail(current):
            self.assertEqual(artifacts.load_reports(self.root)[0]['value'], 1)
            raise RuntimeError('simulated interruption before switch')
        with self.assertRaises(RuntimeError): snapshots.publish(out, self.values(2), before_switch=fail)
        self.assertEqual(snapshots.manifest(out)['snapshot_id'], first['snapshot_id'])
        self.assertTrue(all(r['value'] == 1 for r in artifacts.load_reports(self.root)))
        snapshots.publish(out, self.values(3))
        self.assertEqual(artifacts.read_json(out / 'index.old.json')['value'], 1)
        self.assertTrue(all(r['value'] == 3 for r in artifacts.load_reports(self.root)))

    def test_failed_legacy_migration_preserves_original_json(self):
        out = self.root / '.agentsmd'
        artifacts.write_json(out / 'index.json', self.values(1)['index.json'])
        def fail(current): raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError): snapshots.publish(out, self.values(2), before_switch=fail)
        self.assertEqual(json.loads((out / 'index.json').read_text())['value'], 1)
        self.assertEqual(artifacts.load_reports(self.root)[0]['value'], 1)

    def test_bundle_reader_pins_generation_during_publish(self):
        out = self.root / '.agentsmd'
        snapshots.publish(out, self.values(1))
        original = snapshots.read_report
        called = []
        def interleaved(path, pinned=None):
            value = original(path, pinned)
            if not called:
                called.append(True)
                snapshots.publish(out, self.values(2))
            return value
        with patch.object(snapshots, 'read_report', side_effect=interleaved):
            self.assertEqual([r['value'] for r in artifacts.load_reports(self.root)], [1, 1])

    def test_snapshot_tampering_and_mixed_generations_are_rejected(self):
        out = self.root / '.agentsmd'
        first = snapshots.publish(out, self.values(1))
        first_index = snapshots.resolve(out, 'index.json')
        snapshots.publish(out, self.values(2))
        with self.assertRaises(ValueError):
            artifacts.load_reports(self.root, [first_index, snapshots.resolve(out, 'context.json')])
        current = snapshots.resolve(out, 'index.json')
        current.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            artifacts.read_json(out / 'index.json')

    def test_snapshot_writer_lock_prevents_concurrent_publisher(self):
        out = self.root / '.agentsmd'
        with snapshots.writer_lock(out):
            with self.assertRaisesRegex(ValueError, 'publisher'):
                snapshots.publish(out, self.values(1))

    def test_repeated_symbols_keep_identity_across_reordering(self):
        self.write('a.py', 'def run(x: int): return x+1\ndef run(x: str): return x+"!"\n')
        old = codeindex.scan(self.root)
        self.write('a.py', 'def run(x: str): return x+"!"\ndef run(x: int): return x+1\n')
        new = codeindex.scan(self.root)
        self.assertEqual({s['id'] for s in old['files'][0]['symbols']}, {s['id'] for s in new['files'][0]['symbols']})
        changes, _ = diff.compare(old, new)
        self.assertTrue(all(s['kinds'] == ['location'] for s in changes[0]['symbols']))
        self.assertTrue(changes[0]['conservative'])  # Reordering can alter Python rebinding or Go init order.
        self.write('a.py', 'def run(x: str): return x+"?"\ndef run(x: int): return x+1\n')
        changes, _ = diff.compare(new, codeindex.scan(self.root))
        self.assertEqual(changes[0]['symbols'][0]['kinds'], ['body'])

    def test_ambiguous_duplicate_changes_do_not_guess_by_order(self):
        self.write('a.py', 'def init(): return 1\ndef init(): return 2\n')
        old = codeindex.scan(self.root)
        self.write('a.py', 'def init(): return 3\ndef init(): return 4\n')
        changes, _ = diff.compare(old, codeindex.scan(self.root))
        self.assertTrue(changes[0]['conservative'])
        self.assertTrue(any('ambiguous' in reason for reason in changes[0]['reasons']))
        self.assertFalse(any('body' in s['kinds'] for s in changes[0]['symbols']))

    def test_identical_duplicate_symbols_do_not_report_phantom_changes(self):
        self.write('a.py', 'def init(): return 1\ndef init(): return 1\n')
        before = codeindex.scan(self.root)
        self.assertEqual(diff.compare(before, copy.deepcopy(before))[0], [])
        self.write('a.py', '\n# heading\ndef init(): return 1\ndef init(): return 1\n')
        changes, _ = diff.compare(before, codeindex.scan(self.root))
        self.assertTrue(all(s['kinds'] == ['location'] for s in changes[0]['symbols']))

    def test_selection_reader_resolves_published_manifest(self):
        import scanpolicy
        self.write('service.py', 'def run(): return 1\n')
        selection = scanpolicy.select_files(self.root)
        snapshots.publish(self.root / '.agentsmd', {'selection.json': selection})
        selected = scanpolicy.load_selection(self.root, self.root / '.agentsmd/selection.json')
        self.assertEqual(selected['files'], ['service.py'])

    @unittest.skipUnless(TYPESCRIPT and shutil.which('node'), 'set AGENTSMD_TYPESCRIPT for TypeScript AST tests')
    def test_js_nested_returned_methods_and_complex_object_exports(self):
        self.write('service.ts', 'export function create() {\n function local() { return 1; }\n const client = {send() {return local();}};\n return {load: () => client.send(), nested: {close() {return 0;}}};\n}\nexport default {go() {return 1;}};\n')
        self.write('common.cjs', 'module.exports={send(x) {return x;}, load:()=>1};\n')
        self.write('namespace.ts', 'export namespace N { export function run(){ function local(){return 0;} return local();} }')
        report = codeindex.scan(self.root, js_parser='typescript', typescript=TYPESCRIPT)
        self.assertEqual(report['diagnostics'], [])
        files = {f['path']: f for f in report['files']}
        syms = {s['name']:s for s in files['service.ts']['symbols']}
        for name in ['create.local', 'create.client.send', 'create.<return>.load', 'create.<return>.nested.close', '<default>.go']:
            self.assertIn(name, syms)
        namespaced = {s['name']:s for s in files['namespace.ts']['symbols']}
        self.assertTrue(namespaced['N.run']['exported'])
        self.assertFalse(namespaced['N.run.local']['exported'])
        self.assertFalse(syms['create.local']['exported'])
        self.assertTrue(syms['<default>.go']['exported'])
        self.assertTrue(all(s['exported'] for s in files['common.cjs']['symbols']))

    @unittest.skipUnless(TYPESCRIPT and shutil.which('node'), 'set AGENTSMD_TYPESCRIPT for TypeScript resolution tests')
    def test_tsconfig_aliases_and_reexports_drive_transitive_impact(self):
        self.write('tsconfig.base.json', '{"compilerOptions":{"baseUrl":".","paths":{"@core/*":["src/*"]}}}')
        self.write('tsconfig.json', '{"extends":"./tsconfig.base.json","compilerOptions":{"moduleResolution":"node"}}')
        self.write('src/leaf.ts', 'export function leaf(){return 1;}')
        self.write('src/index.ts', 'export {leaf as run} from "@core/leaf";')
        self.write('app.ts', 'import {run} from "@core/index";\nexport function app(){return run();}')
        self.write('dynamic.ts', 'export function go(name: string) {return import(name);}')
        report = codeindex.scan(self.root, js_parser='typescript', typescript=TYPESCRIPT)
        self.assertEqual(report['diagnostics'], [])
        edges, unresolved = callgraph.file_dependencies(report, self.root)
        self.assertEqual(edges['app.ts'], {'src/index.ts'})
        self.assertEqual(edges['src/index.ts'], {'src/leaf.ts'})
        self.assertTrue(any('non-literal' in x['reason'] for x in unresolved))
        config_files = next(f for f in report['files'] if f['path'] == 'app.ts')['resolution_configs']
        self.assertEqual({c['path'] for c in config_files}, {'tsconfig.json', 'tsconfig.base.json'})
        # Retargeting an inherited alias must stale the index even if source is unchanged.
        import verify
        self.write('src/alternative.ts', 'export function leaf(){return 1;}')
        self.write('tsconfig.base.json', '{"compilerOptions":{"baseUrl":".","paths":{"@core/leaf":["src/alternative.ts"],"@core/*":["src/*"]}}}')
        errors, _, _ = verify.validate(self.root, {'index': report}, [])
        self.assertTrue(any('resolution config changed' in error for error in errors))
        retargeted = codeindex.scan(self.root, js_parser='typescript', typescript=TYPESCRIPT)
        changes, _ = diff.compare(report, retargeted)
        self.assertTrue(any('resolved_imports变化' in reason for change in changes for reason in change['reasons']))
        self.write('tsconfig.base.json', '{"compilerOptions":{"baseUrl":".","paths":{"@core/*":["src/*"]}}}')
        self.write('src/leaf.ts', 'export function leaf(){return 9;}')
        new = codeindex.scan(self.root, js_parser='typescript', typescript=TYPESCRIPT)
        changes, unknown = diff.compare(report, new)
        self.assertIn('app.ts', diff.impact(self.root, report, new, changes, unknown)['files'])


if __name__ == '__main__':
    unittest.main()
