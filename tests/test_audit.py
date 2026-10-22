"""Regressions for complete coverage, authored prose and AST reading hints."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifacts
import candidates
import codeindex
import codemask
import completeness
import scanpolicy
import schemas
import snapshots
import test_publication

TYPESCRIPT = os.environ.get('AGENTSMD_TYPESCRIPT')


class Audit(unittest.TestCase):
    def helper(self):
        helper = test_publication.Publication()
        self.addCleanup(helper.doCleanups)
        return helper

    def test_scope_less_legacy_reports_cannot_certify_complete_coverage(self):
        helper = self.helper(); case = helper.case()
        reports, mapping, module, ledger = case.fixture(); case.complete(mapping, module)
        helper.save(case, reports, mapping, ledger)
        self.assertEqual(helper.cli(case, 'verify.py', '--delivery', 'complete', '--strict', '--json').returncode, 0)
        for report in reports: report.pop('scope', None)
        mapping.pop('scope', None)
        helper.save(case, reports, mapping, ledger)
        # Legacy indexes remain readable, but cannot establish file coverage.
        self.assertEqual(helper.cli(case, 'verify.py', '--json').returncode, 0)
        case.write('unscanned.py', 'def missed(): return 1\n')
        for flag in ('--require-reviewed', '--delivery'):
            args = [flag] if flag == '--require-reviewed' else [flag, 'complete']
            result = helper.cli(case, 'verify.py', *args, '--json')
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertTrue(any('.scope.' in d['message'] for d in json.loads(result.stdout)['diagnostics']))
            self.assertNotIn('Traceback', result.stderr)

    def test_reference_navigation_and_images_are_not_section_explanations(self):
        nav = ('## 项目定位\n[Purpose][p]\n## 架构与数据流\n![Architecture][a]\n'
               '## 构建/运行/测试\n[Commands][]\n[Commands]\n\n'
               '[p]: purpose.md "Project purpose"\n[a]: design.png\n[Commands]: commands.md\n')
        self.assertEqual(len(completeness.section_errors(nav, root=True)), 3)
        explained = nav.replace('[Purpose][p]', '面向本地调用者计算结果；参见 [Purpose][p]。').replace(
            '![Architecture][a]', '入口校验后返回结果；参见 ![Architecture][a]。').replace(
            '[Commands][]', '无需构建；使用 Python，测试未运行。 [Commands][]')
        self.assertEqual(completeness.section_errors(explained, root=True), [])
        self.assertEqual(len(completeness.section_errors(nav + '\n    [Not prose]: extra.md\n', root=True)), 3)

    def test_inline_code_markers_are_examples_and_real_custom_markers_still_work(self):
        example = ('## 自定义\n`<!-- agentsmd:section purpose -->`\nLocal description.\n'
                   '`<!-- agentsmd:placeholder demo -->`\n'
                   '`待核实：根据关键实现补充流程。`\n')
        self.assertFalse(completeness.has_section(example, 'purpose'))
        self.assertEqual(completeness.unfinished(example), [])
        self.assertEqual(len(completeness.section_errors(example, root=True)), 3)
        real = example + '\n<!-- agentsmd:section purpose -->\n面向调用方的本地计算工具。\n'
        self.assertTrue(completeness.has_section(real, 'purpose'))
        self.assertEqual(len(completeness.section_errors(real, root=True)), 2)
        self.assertTrue(completeness.unfinished(real + '\n<!-- agentsmd:placeholder actual -->'))

    def test_malformed_selection_is_rejected_without_traceback_or_replacing_reports(self):
        helper = self.helper(); case = helper.case()
        case.write('service.py', 'def run(): return 1\n')
        baseline = snapshots.publish(case.root / '.agentsmd', {'index.json': codeindex.scan(case.root)})
        current = (case.root / '.agentsmd/current.json').read_bytes()
        valid = scanpolicy.select_files(case.root)
        invalid = [None, [], {'root': str(case.root)}, {**valid, 'files': [None]},
                   {**valid, 'files': ['/service.py']}, {**valid, 'files': ['src/../service.py']},
                   {**valid, 'files': ['service.py', 'service.py']}, {**valid, 'scope': []},
                   {**valid, 'scope': {**valid['scope'], 'options': None}},
                   {**valid, 'scope': {**valid['scope'], 'selection_hash': 'bad'}},
                   {**valid, 'scope': {**valid['scope'], 'selection_hash': '0' * 64}}]
        for value in invalid:
            with self.subTest(value=value):
                path = case.write('selection-input.json', json.dumps(value))
                result = helper.cli(case, 'analyze.py', '--selection', str(path), '--skip-go')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('selection', result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual((case.root / '.agentsmd/current.json').read_bytes(), current)
                self.assertEqual(artifacts.read_json(case.root / '.agentsmd/index.json')['snapshot_id'], baseline['snapshot_id'])

    def test_malformed_generation_and_bundle_metadata_raise_field_errors(self):
        helper = self.helper(); case = helper.case()
        out = case.root / '.agentsmd'
        first = snapshots.publish(out, {'index.json': codeindex.scan(case.root)})
        snapshots.publish(out, {'index.json': codeindex.scan(case.root)})
        current = snapshots.manifest(out)
        for change in ({'artifacts': []}, {'hashes': []}, {'snapshot_id': []}, {'previous': []},
                       {'previous': {**first, 'hashes': []}}, {'agentsmd_generation': 'true'}):
            snapshots.atomic_json(out / 'current.json', {**current, **change})
            with self.assertRaises(schemas.SchemaError): artifacts.read_json(out / 'index.json')
            result = helper.cli(case, 'verify.py', '--json')
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        snapshots.atomic_json(out / 'current.json', current)
        index = snapshots.resolve(out, 'index.json')
        for value in (None, [], {'snapshot_id': current['snapshot_id'], 'hashes': []},
                      {'snapshot_id': current['snapshot_id'], 'hashes': {}}):
            snapshots.atomic_json(index.parent / 'bundle.json', value)
            with self.assertRaises(ValueError): artifacts.read_json(index)

    @unittest.skipUnless(TYPESCRIPT, 'real TypeScript compiler required')
    def test_ast_literals_and_comments_never_supply_failure_candidates(self):
        helper = self.helper(); case = helper.case()
        lines = ['const emoji = "😀";', 'export function probe(x) {',
                 ' if (x) /throw Error/.test(x);', ' while (x) /catch Error/.test(x);',
                 ' const a = "throw Error";', " const b = 'raise Error';",
                 ' const c = `panic ${(() => { throw Error("interpolation"); })()}`;',
                 ' // throw Error("line comment");', ' /*', ' throw Error("block comment");', ' */',
                 ' if (!x) throw Error("real failure");', ' return x / 2;', '}',
                 'export function literal() { return "throw Error"; }',
                 '// throw Error("trailing comment");']
        source = '\n'.join(lines) + '\n'; case.write('probe.ts', source)
        report = codeindex.scan(case.root, js_parser='typescript', typescript=TYPESCRIPT)
        file = next(f for f in report['files'] if f['path'] == 'probe.ts')
        self.assertEqual(schemas.report_errors(report), [])
        self.assertTrue(file['candidate_syntax_valid'])
        mask = codemask.code_mask('probe.ts', source, file['candidate_exclusions'])
        self.assertEqual(len(mask.splitlines()), len(lines))
        files = artifacts.file_map([report]); module = {'files': ['probe.ts']}
        self.assertEqual([r['line'] for r in candidates.candidate_refs(case.root, module, files, 'failure')], [12])
        self.assertEqual([r['line'] for r in candidates.candidate_refs(case.root, module, files, 'success')], [13, 15])
        # Older AST indexes lack the new literal/comment ranges and yield no hints.
        del file['candidate_exclusions']
        self.assertEqual(candidates.candidate_refs(case.root, module, artifacts.file_map([report]), 'failure'), [])

    def test_invalid_ast_exclusion_metadata_is_rejected(self):
        base = {'root': '/fixture', 'files': [{'path': 'a.ts'}]}
        for ranges in (None, [None], [{'start': True, 'end': 4, 'literal': True}],
                       [{'start': 0, 'end': 4}], [{'start': 0, 'end': 4, 'literal': 'yes'}],
                       [{'start': 0, 'end': 4, 'literal': True}, {'start': 3, 'end': 5, 'literal': False}]):
            value = copy.deepcopy(base); value['files'][0]['candidate_exclusions'] = ranges
            self.assertTrue(schemas.report_errors(value), ranges)
        self.assertIsNone(codemask.code_mask('a.ts', '😀', [{'start': 1, 'end': 2, 'literal': True}]))


if __name__ == '__main__': unittest.main()
