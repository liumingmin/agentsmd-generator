"""Regression scenarios for source evidence and generated guide validation.

Run: python3 -m unittest discover -s tests -v
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import callgraph
import codeindex
import diff
import projectcontext
import verify


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def indexed(self, path, **kwargs):
        raw = (self.root / path).read_bytes()
        return {'path': path, 'line_end': len(raw.splitlines()),
                'content_hash': hashlib.sha256(raw).hexdigest(), 'symbols': [], **kwargs}

    def report(self, files):
        return {'schema_version': 2, 'root': str(self.root), 'files': files,
                'stats': {'file_count': len(files), 'package_count': 1,
                          'symbol_count': sum(len(f['symbols']) for f in files)}}

    def script(self, script, *args):
        return subprocess.run([sys.executable, str(REPO / 'scripts' / script), *map(str, args)],
                              text=True, capture_output=True, check=True)

    def test_context_discovers_ci_nested_manifests_tests_and_environment_keys(self):
        self.write('.github/workflows/test.yml', 'jobs:\n  test:\n    steps: []\n')
        self.write('web/package.json', json.dumps({'name': 'web', 'scripts': {'test': 'node --test'},
                                                  'dependencies': {'@scope/lib': '^2.1'}}))
        self.write('api/go.mod', 'module example.com/api\ngo 1.24.3\nrequire (\n example.com/x v1.2.3\n)\n')
        self.write('tests/scenario.py', 'def test_reject():\n    assert True\n')
        self.write('.env', 'TOKEN=do-not-copy-this\n')
        self.write('node_modules/ignored.js', 'function ignored() {}')
        self.write('migrations/001.sql', 'CREATE TABLE events(id INT);\n')
        self.write('AGENTS.md', 'generated document')
        report = projectcontext.scan(self.root)
        self.assertIn('.github/workflows/test.yml', report['categories']['ci'])
        self.assertIn('tests/scenario.py', report['categories']['source'])
        self.assertIn('tests/scenario.py', report['categories']['test'])
        paths = {f['path']: f for f in report['files']}
        self.assertNotIn('node_modules/ignored.js', paths)
        self.assertNotIn('AGENTS.md', paths)
        self.assertEqual(paths['.env']['environment_keys'], [{'name': 'TOKEN', 'line': 1}])
        self.assertNotIn('do-not-copy-this', json.dumps(report))
        self.assertIn({'kind': 'require', 'value': 'example.com/x v1.2.3', 'line': 4}, paths['api/go.mod']['facts'])
        self.assertIn({'kind': 'scripts', 'name': 'test', 'value': 'node --test',
                       'json_pointer': '/scripts/test'}, paths['web/package.json']['facts'])

    def test_context_limits_and_parse_failures_are_visible(self):
        self.write('package.json', '{bad json')
        self.write('big.py', 'x' * 100)
        self.write('raw.sql', '\x00\x00')
        report = projectcontext.scan(self.root, max_bytes=50)
        diagnostics = {d['path'] for d in report['diagnostics']}
        self.assertIn('package.json', diagnostics)
        self.assertIn('big.py', diagnostics)

    def test_python_ast_handles_multiline_signatures_methods_and_fake_definitions(self):
        text = ('from .storage import Save\n'
                'TEXT = """\ndef fake(): pass\n"""\n'
                'class Service:\n'
                '    @staticmethod\n'
                '    async def load(\n'
                '        key: str,\n'
                '        timeout: int = 10,\n'
                '    ) -> list[str]:\n'
                '        """Load records."""\n'
                '        return [key]\n')
        path = self.write('service.py', text)
        file = codeindex.scan_file(path, 'service.py', 'python', [])
        symbols = {s['name']: s for s in file['symbols']}
        self.assertNotIn('fake', symbols)
        method = symbols['Service.load']
        self.assertEqual((method['line'], method['end_line']), (7, 12))
        self.assertEqual(method['results'], 'list[str]')
        self.assertIn('timeout: int=10', method['params'])
        self.assertEqual(method['doc'], 'Load records.')
        self.assertEqual(file['imports'], ['.storage'])
        self.assertEqual(file['content_hash'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_index_cli_diagnoses_invalid_python_and_groups_root_files(self):
        self.write('valid.py', 'def run():\n    return 1\n')
        self.write('bad.py', 'def broken(:\n')
        self.write('test_case.py', 'def test_ok():\n    pass\n')
        report = json.loads(self.script('codeindex.py', '--root', self.root).stdout)
        self.assertEqual(report['diagnostics'][0]['path'], 'bad.py')
        self.assertEqual(report['stats']['package_count'], 1)
        self.assertEqual(report['packages'][0]['dir'], '')
        self.assertEqual(report['stats']['file_count'], 2)
        self.assertTrue(next(f for f in report['files'] if f['path'] == 'test_case.py')['is_test'])

    def test_regex_does_not_invent_exports_or_function_spans(self):
        path = self.write('src/view.ts', '/*\nfunction fake() {}\n*/\nconst value = (1 + 2);\n'
                          'export const go = (key: string) => {\n return key;\n};\n')
        file = codeindex.scan_file(path, 'src/view.ts', 'typescript', codeindex.RULES['typescript'][1])
        self.assertEqual([s['name'] for s in file['symbols']], ['go'])
        self.assertIsNone(file['symbols'][0]['exported'])
        self.assertEqual(file['symbols'][0]['line'], file['symbols'][0]['end_line'])

    def test_verify_nested_mixed_docs_config_evidence_and_scoped_stats(self):
        self.write('src/a.go', 'package a\nfunc Run() {\n println(1)\n}\n// trailing comment\n')
        self.write('web/index.ts', 'const internal = 1;\nexport const run = () => internal;\n')
        self.write('config.yaml', 'port: 8080\n')
        self.write('docs/AGENTS.api.md', '## 功能点清单\n[src/a.go:L3-L5](../src/a.go#L3-L5)\n'
                   '[config.yaml:L1](../config.yaml#L1)\n## 依赖与协作\n## 测试与验证\n')
        self.write('AGENTS.md', '## 模块地图\n[API](docs/AGENTS.api.md)\n## 架构与数据流\n'
                   '## 构建/运行/测试\n[web/index.ts:L2](web/index.ts#L2)\n'
                   '<!-- agentsmd:stats {"source":".agentsmd/ast.json","file_count":1} -->\n')
        reports = {'.agentsmd/ast.json': self.report([self.indexed('src/a.go')]),
                   '.agentsmd/index.json': self.report([self.indexed('web/index.ts')])}
        for path, report in reports.items():
            self.write(path, json.dumps(report))
        result = self.script('verify.py', '--root', self.root, '--strict')
        self.assertIn('索引 2 份', result.stdout)
        self.assertIn('0 个错误', result.stdout)

    def test_verify_rejects_missing_ambiguous_short_path_and_bad_ranges(self):
        self.write('a/file.go', 'package a\nfunc A() {}\n')
        self.write('b/file.go', 'package b\nfunc B() {}\n')
        self.write('AGENTS.md', '[file.go:L2](a/file.go)\n[a/file.go:L2-L99](a/file.go)\n'
                   '[a/file.go:L2-L1](a/file.go)\n[a/file.go:L0](a/file.go)\n')
        reports = {'ast': self.report([self.indexed('a/file.go'), self.indexed('b/file.go')])}
        errors, _, _ = verify.validate(self.root, reports, ['AGENTS.md'])
        self.assertTrue(any('file.go' in e and '无法读取' in e for e in errors))
        self.assertEqual(sum('超出有效范围' in e for e in errors), 3)
        self.assertTrue(any('不一致' in e for e in errors))

    def test_verify_detects_same_line_count_stale_index_and_wrong_links(self):
        self.write('one.py', 'def f(): return 1\n')
        self.write('two.py', 'def g(): return 2\n')
        report = self.report([self.indexed('one.py')])
        self.write('one.py', 'def f(): return 9\n')
        self.write('AGENTS.md', '[one.py:L1](two.py#L2)\n'
                   '<!-- agentsmd:stats {"source":"ast","file_count":99} -->\n')
        errors, _, _ = verify.validate(self.root, {'ast': report}, ['AGENTS.md'])
        self.assertTrue(any('过期' in e for e in errors))
        self.assertTrue(any('链接目标不一致' in e for e in errors))
        self.assertTrue(any('锚点不一致' in e for e in errors))
        self.assertTrue(any('file_count' in e for e in errors))

    def test_diff_detects_body_config_field_and_route_location_changes(self):
        self.write('service.py', 'def save(): return 1\n')
        before = self.report([self.indexed('service.py')])
        self.write('service.py', 'def save(): return 2\n')
        after = self.report([self.indexed('service.py')])
        changes, unknown = diff.compare(before, after)
        self.assertEqual([c['path'] for c in changes], ['service.py'])
        self.assertEqual(unknown, [])
        self.write('config.yaml', 'port: 1\n')
        ctx1 = projectcontext.scan(self.root)
        self.write('config.yaml', 'port: 2\n')
        ctx2 = projectcontext.scan(self.root)
        changes, _ = diff.compare(ctx1, ctx2)
        self.assertIn('config.yaml', {c['path'] for c in changes})
        before = self.report([self.indexed('service.py', symbols=[
            {'name': 'Record', 'kind': 'struct', 'line': 1, 'end_line': 1,
             'fields': [{'name': 'ID', 'type': 'int', 'tag': 'json:"id"', 'line': 1}]}])])
        after = copy.deepcopy(before)
        after['files'][0]['symbols'][0]['fields'][0]['type'] = 'string'
        changes, _ = diff.compare(before, after)
        self.assertTrue(changes)
        before['routes'] = [{'file': 'service.py', 'line': 1, 'path': '/a', 'method': 'GET', 'handler': 'f'}]
        after = copy.deepcopy(before)
        after['routes'][0]['line'] = 2
        changes, _ = diff.compare(before, after)
        self.assertEqual(len(changes), 2)

    def test_diff_finds_nested_links_for_deleted_config(self):
        self.write('docs/AGENTS.api.md', '[configuration](../config.yaml)\n')
        hits = diff.affected_docs(self.root, {'config.yaml'})
        self.assertEqual(hits, {'docs/AGENTS.api.md': [(1, 'config.yaml')]})
        old = {'files': [{'path': 'a.py', 'symbols': []}]}
        changes, unknown = diff.compare(old, old)
        self.assertEqual(changes, [])
        self.assertEqual(unknown, ['a.py'])

    def test_import_graph_uses_exact_modules_and_supports_root_and_nested_packages(self):
        self.write('go.mod', 'module example.com/root\n')
        self.write('nested/go.mod', 'module example.com/nested\n')
        report = {'files': [
            {'path': 'root.go', 'imports': []},
            {'path': 'store/store.go', 'imports': []},
            {'path': 'cmd/main.go', 'imports': ['example.com/root', 'example.com/root/store',
                                               'external.org/store', 'example.com/root/store/missing',
                                               'example.com/nested']},
            {'path': 'nested/lib.go', 'imports': []}]}
        edges = callgraph.import_edges(report, self.root)
        self.assertEqual(edges['cmd'], {'.', 'store', 'nested'})
        self.assertEqual(callgraph.import_edges(report, self.root / 'missing'), {})


if __name__ == '__main__':
    unittest.main()
