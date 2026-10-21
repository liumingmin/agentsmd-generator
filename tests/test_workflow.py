"""End-to-end invariants for scope, AST, ownership, evidence and incremental review."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import codeindex
import projectcontext
import scanpolicy
import modules
import evidence
import diff
import callgraph
from artifacts import write_json, load_reports

TYPESCRIPT = os.environ.get('AGENTSMD_TYPESCRIPT')


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def reports(self):
        selection = scanpolicy.select_files(self.root)
        return [projectcontext.scan(self.root, selection=selection), codeindex.scan(self.root, selection, js_parser='regex')]

    def docs(self, mapping):
        by_id = {m['id']: m for m in mapping['modules']}
        for module in mapping['modules']:
            neighbors = module['children'] + ([module['parent']] if module['parent'] else [])
            self.write(module['document'], '\n'.join(f"[{mid}]({by_id[mid]['document']})" for mid in neighbors))

    def fixture(self):
        self.write('service.py', 'def run(value: int) -> int:\n    return value + 1\n\ndef untouched():\n    return 3\n')
        self.write('caller.py', 'from service import run\ndef call():\n    return run(1)\n')
        reports = self.reports()
        mapping = modules.plan(self.root, reports)
        self.docs(mapping)
        self.write('AGENTS.md', '<!-- agentsmd:claim service-run -->\nrun 返回输入加一。\n<!-- agentsmd:endclaim -->\n')
        symbol = reports[1]['files'][1]['symbols'][0]
        self.assertEqual(symbol['name'], 'run')
        draft = {'claims': [{'id': 'service-run', 'module': '.', 'document': 'AGENTS.md', 'kind': 'behavior',
                             'status': 'confirmed', 'evidence': [{'role': 'implementation', 'path': 'service.py',
                             'line': 1, 'end_line': 2, 'symbol_id': symbol['id']}]}]}
        ledger = evidence.capture(self.root, draft, reports)
        return reports, mapping, ledger

    def test_selection_gitignore_config_and_categories_are_shared(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.write('.gitignore', 'ignored.py\ntracked.py\n')
        self.write('ignored.py', 'pass\n')
        self.write('tracked.py', 'pass\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '-f', 'tracked.py'], check=True)
        self.write('src/app.py', 'pass\n')
        self.write('tests/generated_test.py', '# Code generated fixture DO NOT EDIT\npass\n')
        self.write('src/generated.py', '# Code generated fixture DO NOT EDIT\npass\n')
        self.write('src/exclude.py', 'pass\n')
        self.write('.github/workflows/ci.yml', 'jobs: {}\n')
        self.write('.hidden/secret.py', 'pass\n')
        self.write('.agentsmd/config.json', json.dumps({'exclude': ['src/exclude.py']}))
        selection = scanpolicy.select_files(self.root)
        self.assertIn('tracked.py', selection['files'])
        self.assertNotIn('ignored.py', selection['files'])
        self.assertNotIn('src/exclude.py', selection['files'])
        self.assertNotIn('.hidden/secret.py', selection['files'])
        self.assertIn('.github/workflows/ci.yml', selection['files'])
        context = projectcontext.scan(self.root, selection=selection)
        index = codeindex.scan(self.root, selection, js_parser='regex')
        self.assertEqual(context['scope'], index['scope'])
        self.assertEqual({f['path'] for f in index['files']}, set(context['categories']['source']))
        self.assertEqual(index['stats']['by_category']['generated']['file_count'], 1)
        self.assertEqual(index['stats']['by_category']['test']['file_count'], 1)
        # Git subdirectory paths must stay relative to the requested root.
        nested = scanpolicy.select_files(self.root / 'src')
        self.assertIn('app.py', nested['files'])
        self.assertNotIn('src/app.py', nested['files'])

    def test_scope_rejects_bad_config_manifest_and_symlinks(self):
        self.write('ok.py', 'pass\n')
        selection = scanpolicy.select_files(self.root)
        invalid = copy.deepcopy(selection)
        invalid['files'].append('../outside.py')
        with self.assertRaises(ValueError): scanpolicy.load_selection(self.root, invalid)
        self.write('.agentsmd/config.json', '{"include_hidden":false}')
        with self.assertRaises(ValueError): scanpolicy.select_files(self.root)
        (self.root / '.agentsmd/config.json').unlink()
        (self.root / 'link.py').symlink_to(self.root / 'ok.py')
        selected = scanpolicy.select_files(self.root)
        self.assertNotIn('link.py', selected['files'])
        self.assertTrue(any(d['path'] == 'link.py' for d in selected['diagnostics']))

    def test_python_fingerprints_and_duplicate_declarations(self):
        self.write('a.py', 'def run(x):\n return x + 1\ndef run(x):\n return x + 2\n')
        before = codeindex.scan(self.root)
        self.write('a.py', '\n# comment\ndef run( x ):\n    return x+1\ndef run(x):\n    return x+2\n')
        after = codeindex.scan(self.root)
        a, b = before['files'][0]['symbols'], after['files'][0]['symbols']
        self.assertEqual(len({s['id'] for s in a}), 2)
        self.assertEqual([s['body_hash'] for s in a], [s['body_hash'] for s in b])
        changes, _ = diff.compare(before, after)
        self.assertTrue(all(s['kinds'] == ['location'] for s in changes[0]['symbols']))
        self.write('a.py', 'def run(x):\n return x + 9\ndef run(x):\n return x + 2\n')
        changed, _ = diff.compare(before, codeindex.scan(self.root))
        self.assertEqual(changed[0]['symbols'][0]['kinds'], ['body'])

    def test_module_coverage_split_navigation_and_unreviewed_status(self):
        for i in range(4): self.write(f'provider/storage/f{i}.py', 'def run(): pass\n')
        self.write('provider/network/main.py', 'def run(): pass\n')
        self.write('tools/task.ps1', 'Write-Output ok\n')  # unsupported syntax still needs ownership
        reports = self.reports()
        mapping = modules.plan(self.root, reports, max_files=2)
        by_id = {m['id']: m for m in mapping['modules']}
        self.assertIn('provider/storage', by_id)
        self.assertTrue(by_id['provider/storage']['needs_manual_split'])
        self.assertEqual(mapping['source_count'], 6)
        self.docs(mapping)
        errors, warnings = modules.check(self.root, mapping, reports)
        self.assertEqual(errors, [])
        self.assertTrue(any('unreviewed' in w for w in warnings))
        errors, _ = modules.check(self.root, mapping, reports, require_reviewed=True)
        self.assertTrue(any('unreviewed' in e for e in errors))
        by_id['provider/storage']['files'].pop()
        by_id['tools']['files'].append('provider/network/main.py')
        errors, _ = modules.check(self.root, mapping, reports)
        self.assertTrue(any('uncovered source' in e for e in errors))
        self.assertTrue(any('duplicate ownership' in e for e in errors))
        self.write('AGENTS.md', 'no links')
        errors, _ = modules.check(self.root, mapping, reports)
        self.assertTrue(any('navigation' in e for e in errors))

    def test_evidence_binds_prose_roles_and_live_source(self):
        reports, mapping, ledger = self.fixture()
        self.assertEqual(evidence.check(self.root, ledger, mapping, reports)[0], [])
        changed = copy.deepcopy(ledger)
        changed['claims'][0]['kind'] = 'flow'
        errors, _ = evidence.check(self.root, changed, mapping, reports)
        self.assertTrue(any('needs roles caller, callee' in e or 'needs roles callee, caller' in e for e in errors))
        self.write('AGENTS.md', '<!-- agentsmd:claim service-run -->\n声称自动重试。\n<!-- agentsmd:endclaim -->\n')
        errors, _ = evidence.check(self.root, ledger, mapping, reports)
        self.assertTrue(any('changed Markdown' in e for e in errors))
        self.write('service.py', 'def run(value: int) -> int:\n    return value + 9\n')
        errors, _ = evidence.check(self.root, ledger, mapping, self.reports())
        self.assertTrue(any('stale snippet_hash' in e for e in errors))

    def test_uncertainty_must_be_visible_and_execution_must_be_recorded(self):
        reports, mapping, ledger = self.fixture()
        claim = ledger['claims'][0]
        claim['status'] = 'inferred'
        errors, _ = evidence.check(self.root, ledger, mapping, reports)
        self.assertTrue(any('needs a reason' in e for e in errors))
        self.assertTrue(any('visible in prose' in e for e in errors))
        mapping['modules'][0]['review']['test'] = {'status': 'passed', 'reason': 'claimed run'}
        errors, _ = modules.check(self.root, mapping, reports)
        self.assertTrue(any('execution metadata' in e for e in errors))

    def test_diff_range_filter_and_transitive_dependents(self):
        reports, mapping, ledger = self.fixture()
        old = reports[1]
        self.write('third.py', 'from caller import call\ndef use(): return call()\n')
        old = codeindex.scan(self.root)
        self.write('service.py', 'def run(value: int) -> int:\n    return value + 1\n\ndef untouched():\n    return 9\n')
        new = codeindex.scan(self.root)
        changes, unknown = diff.compare(old, new)
        effects = diff.impact(self.root, old, new, changes, unknown, mapping, ledger)
        self.assertEqual(effects['files'], ['caller.py', 'service.py', 'third.py'])
        self.assertEqual(effects['claims'], [])  # only the other symbol changed
        self.write('service.py', 'def run(value: int) -> int:\n    return value + 9\n\ndef untouched():\n    return 3\n')
        new = codeindex.scan(self.root)
        changes, unknown = diff.compare(old, new)
        effects = diff.impact(self.root, old, new, changes, unknown, mapping, ledger)
        self.assertEqual([c['id'] for c in effects['claims']], ['service-run'])

    def test_top_level_changes_force_conservative_claim_review(self):
        self.write('a.py', 'LIMIT=1\ndef run(): return LIMIT\n')
        old = codeindex.scan(self.root)
        self.write('a.py', 'LIMIT=9\ndef run(): return LIMIT + 1\n')
        changes, _ = diff.compare(old, codeindex.scan(self.root))
        self.assertTrue(changes[0]['conservative'])

    def test_js_dependency_resolves_reexports_index_and_emitted_extensions(self):
        report = {'files': [{'path': 'src/a.ts', 'imports': ['./lib', './util.js', '@alias/x']},
                            {'path': 'src/lib/index.ts', 'imports': ['./other']},
                            {'path': 'src/util.ts', 'imports': []}]}
        edges, unresolved = callgraph.file_dependencies(report, self.root)
        self.assertEqual(edges['src/a.ts'], {'src/lib/index.ts', 'src/util.ts'})
        self.assertTrue(any(x['import'] == '@alias/x' for x in unresolved))

    def test_js_parser_unavailable_is_explicit(self):
        self.write('a.ts', 'export function run() {}\n')
        report = codeindex.scan(self.root, node='agentsmd-no-such-node')
        self.assertIn('regex fallback', report['notices'][0])
        self.assertEqual(report['files'][0]['parser']['name'], 'regex')
        with self.assertRaises(ValueError): codeindex.scan(self.root, js_parser='typescript', node='agentsmd-no-such-node')

    @unittest.skipUnless(TYPESCRIPT and shutil.which('node'), 'set AGENTSMD_TYPESCRIPT to test compiler backend')
    def test_js_ast_multiline_methods_exports_commonjs_and_invalid_syntax(self):
        self.write('view.ts', 'import { x } from "./lib";\nexport { x as value } from "./lib";\n'
                   'export const run = async (\n key: string,\n) : Promise<string> => {\n return key;\n};\n'
                   'export class Service {\n private secret() { return 1; }\n #hidden() { return 2; }\n load(key: string) { return run(key); }\n}\n')
        self.write('common.cjs', 'function local(x) { return x; }\nmodule.exports = {local};\nexports.inline = x => x+1;\n')
        self.write('bad.ts', 'export function bad( {\n')
        report = codeindex.scan(self.root, js_parser='typescript', typescript=TYPESCRIPT)
        files = {f['path']: f for f in report['files']}
        self.assertNotIn('bad.ts', files)
        self.assertTrue(any(d['path'] == 'bad.ts' for d in report['diagnostics']))
        symbols = {s['name']: s for s in files['view.ts']['symbols']}
        self.assertEqual((symbols['run']['line'], symbols['run']['end_line']), (3, 7))
        self.assertIn('key: string', symbols['run']['params'])
        self.assertEqual(symbols['run']['results'], 'Promise<string>')
        self.assertFalse(symbols['Service.secret']['exported'])
        self.assertFalse(symbols['Service.#hidden']['exported'])
        self.assertTrue(symbols['Service.load']['exported'])
        self.assertEqual(files['view.ts']['imports'], ['./lib'])
        common = {s['name']: s for s in files['common.cjs']['symbols']}
        self.assertTrue(common['local']['exported'])
        self.assertTrue(common['exports.inline']['exported'])
        old = report
        source = (self.root / 'view.ts').read_text()
        self.write('view.ts', '\n// comment\n' + source.replace('return key;', 'return  key ;'))
        new = codeindex.scan(self.root, js_parser='typescript', typescript=TYPESCRIPT)
        old_hashes = [s['content_hash'] for s in files['view.ts']['symbols']]
        self.assertEqual(old_hashes, [s['content_hash'] for s in next(f for f in new['files'] if f['path'] == 'view.ts')['symbols']])
        changes, _ = diff.compare(old, new)
        self.assertFalse(any('body' in s['kinds'] or 'contract' in s['kinds'] for c in changes for s in c['symbols']))

    def test_declaration_and_test_only_modules_need_no_invented_runtime_flow(self):
        for path, source, kind, role in [
            ('schema.proto', 'message Entry {}\n', 'interface', 'interface'),
            ('test_example.py', 'def test_ok(): assert True\n', 'test', 'test')]:
            self.write(path, source)
            reports = self.reports()
            mapping = modules.plan(self.root, reports)
            self.write('AGENTS.md', '<!-- agentsmd:claim primary -->\n已核实的声明或断言。\n<!-- agentsmd:endclaim -->\n')
            ledger = evidence.capture(self.root, {'claims': [{'id': 'primary', 'module': '.',
                'document': 'AGENTS.md', 'kind': kind, 'status': 'confirmed',
                'evidence': [{'role': role, 'path': path, 'line': 1}]}]}, reports)
            self.assertEqual(evidence.check(self.root, ledger, mapping, reports, require_modules=True)[0], [])
            (self.root / path).unlink()

    def test_manifest_change_reviews_only_its_source_subtree(self):
        self.write('web/package.json', '{"type":"module"}')
        self.write('web/main.js', 'export const value = 1;')
        self.write('other/main.py', 'def run(): pass')
        old = projectcontext.scan(self.root)
        self.write('web/package.json', '{"type":"commonjs"}')
        new = projectcontext.scan(self.root)
        changes, unknown = diff.compare(old, new)
        effects = diff.impact(self.root, old, new, changes, unknown)
        self.assertIn('web/main.js', effects['files'])
        self.assertNotIn('other/main.py', effects['files'])

    def test_verify_full_delivery_and_scope_drift(self):
        reports, mapping, ledger = self.fixture()
        mapping['modules'][0]['review'] = {'entrypoint': ['service.py'], 'implementation': ['service.py'],
                                         'test': {'status': 'not_found', 'reason': 'Searched selected fixture sources; none are tests'}}
        with (self.root / 'AGENTS.md').open('a') as stream:
            stream.write('\n## 模块地图\n## 架构与数据流\n## 构建/运行/测试\n')
        for name, report in zip(('context', 'index'), reports):
            write_json(self.root / f'.agentsmd/{name}.json', report)
        write_json(self.root / '.agentsmd/modules.json', mapping)
        write_json(self.root / '.agentsmd/evidence.json', ledger)
        # Existing unrelated instruction templates should keep their original purpose.
        self.write('templates/AGENTS.md', 'Original instructions without guide headings.\n')
        command = [sys.executable, str(REPO / 'scripts/verify.py'), '--root', str(self.root), '--require-reviewed', '--strict']
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write('new.py', 'def new(): pass\n')
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('selection is stale', result.stdout)

    def test_duplicate_claim_blocks_across_documents_are_rejected(self):
        reports, mapping, ledger = self.fixture()
        self.write('AGENTS.child.md', '<!-- agentsmd:claim service-run -->\nDuplicate\n<!-- agentsmd:endclaim -->\n')
        mapping['modules'].append({'id': 'child', 'document': 'AGENTS.child.md', 'files': []})
        errors, _ = evidence.check(self.root, ledger, mapping, reports)
        self.assertTrue(any('duplicated across documents' in error for error in errors))

    def test_old_module_snapshot_survives_current_manifest_rename(self):
        self.write('go.mod', 'module example.com/new\n')
        report = {'local_modules': {'.': 'example.com/old'}, 'files': [
            {'path': 'lib/lib.go', 'imports': []},
            {'path': 'main.go', 'imports': ['example.com/old/lib']}]}
        self.assertEqual(callgraph.import_edges(report, self.root)['.'], {'lib'})

    def test_pipeline_keeps_review_artifacts_and_baseline(self):
        self.write('service.py', 'def run(): return 1\n')
        self.write('.agentsmd/modules.json', '{"manual":true}\n')
        self.write('.agentsmd/evidence.json', '{"manual":true}\n')
        cmd = [sys.executable, str(REPO / 'scripts/analyze.py'), '--root', str(self.root), '--skip-go']
        subprocess.run(cmd, check=True, capture_output=True)
        self.write('service.py', 'def run(): return 9\n')
        subprocess.run(cmd, check=True, capture_output=True)
        self.assertEqual(json.loads((self.root / '.agentsmd/modules.json').read_text()), {'manual': True})
        self.assertEqual(json.loads((self.root / '.agentsmd/evidence.json').read_text()), {'manual': True})
        self.assertTrue((self.root / '.agentsmd/index.old.json').exists())
        reports = load_reports(self.root)
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]['scope'], reports[1]['scope'])


if __name__ == '__main__':
    unittest.main()
