"""Regression tests for write scope, review freshness, completion and diagnostics."""
from argparse import Namespace
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import os
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import test_quality
import artifacts
import completeness
import diagnostics
import draft
import evidence
import modules
import reviewstate
import schemas

REPO = Path(__file__).resolve().parents[1]


class Delivery(unittest.TestCase):
    def fixture(self):
        case = test_quality.Quality()
        case.setUp()
        self.addCleanup(case.doCleanups)
        reports, mapping, module, ledger = case.fixture()
        case.complete(mapping, module)
        return case, reports, mapping, module, ledger

    def saved(self, case, reports, mapping, ledger):
        for name, value in [('context', reports[0]), ('index', reports[1]), ('modules', mapping), ('evidence', ledger)]:
            artifacts.write_json(case.root / f'.agentsmd/{name}.json', value)

    def cli(self, case, script, *args):
        return subprocess.run([sys.executable, str(REPO / 'scripts' / script), '--root', str(case.root), *args],
                              text=True, capture_output=True)

    def test_all_guide_targets_are_checked_before_any_write(self):
        case, reports, mapping, module, ledger = self.fixture()
        original = {p: p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
        for invalid in ('src/service.py', 'README.md', '.git/AGENTS.bad.md', '.agentsmd/snapshots/AGENTS.bad.md', '../AGENTS.bad.md'):
            with self.subTest(target=invalid):
                bad = copy.deepcopy(mapping)
                bad['modules'][1]['document'] = invalid
                with self.assertRaises(ValueError): draft.generate(case.root, bad, reports, ledger, write=True)
                self.assertEqual({p:p.read_bytes() for p in original}, original)
        # Case aliases are rejected even on case-sensitive filesystems.
        bad = copy.deepcopy(mapping)
        bad['modules'][1]['document'] = 'agents.md'
        with self.assertRaisesRegex(ValueError, 'duplicate guide target'):
            draft.generate(case.root, bad, reports, write=True)
        self.assertEqual({p:p.read_bytes() for p in original}, original)
        errors, _ = modules.check(case.root, bad, reports)
        self.assertTrue(any('duplicate guide target' in e for e in errors))

    def test_symlink_directory_file_and_hardlink_targets_cannot_change_source(self):
        case, reports, mapping, module, _ = self.fixture()
        source = case.root / 'src/service.py'
        original = source.read_bytes()
        (case.root / 'AGENTS.link.md').symlink_to(source)
        (case.root / 'linked').symlink_to(case.root / 'src', target_is_directory=True)
        os.link(source, case.root / 'AGENTS.hard.md')
        for document in ('AGENTS.link.md', 'linked/AGENTS.child.md', 'AGENTS.hard.md'):
            with self.subTest(document=document):
                bad = copy.deepcopy(mapping)
                bad['modules'][1]['document'] = document
                with self.assertRaises(ValueError): draft.generate(case.root, bad, reports, write=True)
                self.assertEqual(source.read_bytes(), original)
        self.assertFalse((case.root / 'src/AGENTS.child.md').exists())

    def test_preview_and_companion_artifacts_stay_in_analysis_directory(self):
        case, reports, mapping, module, _ = self.fixture()
        for directory in ('src', '.', '.agentsmd/snapshots', '/tmp', '.agentsmd/drafts/../snapshots'):
            with self.subTest(directory=directory):
                with self.assertRaises(ValueError): draft.generate(case.root, mapping, reports, out_dir=directory)
        source = case.root / 'src/service.py'
        original = source.read_bytes()
        (case.root / '.agentsmd').mkdir()
        (case.root / '.agentsmd/evidence.draft.json').symlink_to(source)
        with self.assertRaises(ValueError): draft.generate(case.root, mapping, reports, write=True)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((case.root / '.agentsmd/modules.draft.json').exists())

    def test_new_function_outside_bound_evidence_expires_complete_review(self):
        case, reports, mapping, module, ledger = self.fixture()
        self.assertEqual(modules.check(case.root, mapping, reports, require_reviewed=True)[0], [])
        old_hashes = copy.deepcopy(module['review']['source_hashes'])
        source = case.root / 'src/service.py'
        source.write_text(source.read_text() + '\ndef new_feature(value): return value * 9\n')
        refreshed = case.reports()
        # Reindexing and recapturing the unchanged original claim cannot refresh reading.
        ledger = evidence.capture(case.root, ledger, refreshed)
        errors, _ = modules.check(case.root, mapping, refreshed, require_reviewed=True)
        self.assertTrue(any('review stale source' in e for e in errors))
        self.assertTrue(any('review stale source' in e for e in evidence.check(case.root, ledger, mapping, refreshed, True)[0]))
        reread = reviewstate.record(case.root, mapping, refreshed, [module['id']])
        self.assertEqual(modules.check(case.root, reread, refreshed, require_reviewed=True)[0], [])
        self.assertEqual(evidence.check(case.root, ledger, reread, refreshed, True)[0], [])
        self.assertEqual(module['review']['source_hashes'], old_hashes)

    def test_reading_disposition_and_execution_status_are_not_promoted_by_record(self):
        case, reports, mapping, module, _ = self.fixture()
        module['review']['scope'] = 'sampled'
        before = copy.deepcopy(module['review'])
        stamped = reviewstate.record(case.root, mapping, reports, [module['id']])
        after = stamped['modules'][1]['review']
        for key in ('scope', 'reviewed_files', 'capabilities', 'test'):
            self.assertEqual(after[key], before[key])
        module['review']['scope'] = 'unreviewed'
        with self.assertRaisesRegex(ValueError, 'declare sampled/complete'):
            reviewstate.record(case.root, mapping, reports, [module['id']])
        source = case.root / 'src/service.py'
        source.write_text(source.read_text() + '\n# changed\n')
        module['review']['scope'] = 'sampled'
        with self.assertRaisesRegex(ValueError, 'source index stale'):
            reviewstate.record(case.root, mapping, reports, [module['id']])

    def test_legacy_review_without_hashes_is_reported_not_silently_filled(self):
        case, reports, mapping, module, _ = self.fixture()
        module['review'].pop('source_hashes')
        errors, _ = modules.check(case.root, mapping, reports, require_reviewed=True)
        self.assertTrue(any('review snapshot missing' in e for e in errors))
        module['review']['scope'] = 'sampled'
        with (case.root / module['document']).open('a') as stream: stream.write('\n抽样说明。\n')
        errors, warnings = modules.check(case.root, mapping, reports)
        self.assertEqual(errors, [])
        self.assertTrue(any('review snapshot missing' in w for w in warnings))
        self.assertNotIn('source_hashes', module['review'])

    def test_generator_placeholders_cannot_pass_full_strict_delivery(self):
        case, reports, mapping, module, ledger = self.fixture()
        (case.root / 'AGENTS.md').write_text(f'[src]({module["document"]})\n')
        draft.generate(case.root, mapping, reports, ledger, write=True)
        self.saved(case, reports, mapping, ledger)
        result = self.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(any(d['code'] == 'draft.incomplete' for d in data['diagnostics']))
        self.assertNotIn('Traceback', result.stderr)
        self.assertTrue(any(d['module'] == '.' for d in data['diagnostics']))

    def test_reviewed_prose_and_explicit_unknowns_can_pass_full_delivery(self):
        case, reports, mapping, module, ledger = self.fixture()
        (case.root / 'AGENTS.md').write_text(f'[src]({module["document"]})\n')
        draft.generate(case.root, mapping, reports, ledger, write=True)
        replacements = {
            'purpose': '用于演示参数校验和计算的独立样例。',
            'architecture': '入口校验参数后计算结果。',
            'commands': '本样例无构建工具；未执行目标测试。',
            'collaboration': '无外部调用，父级导览负责导航。',
            'tests': '未执行；原因：本次仅验证导览工具。'}
        import re
        for entry in mapping['modules']:
            doc = case.root / entry['document']
            text = doc.read_text()
            for key, prose in replacements.items():
                text = re.sub(r'<!-- agentsmd:placeholder ' + key + r' -->.*?<!-- agentsmd:endplaceholder -->', prose, text, flags=re.S)
            doc.write_text(text + '\n## 待核实事项\n线上部署环境尚未确认，本次未访问线上。\n')
        self.saved(case, reports, mapping, ledger)
        result = self.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['error_count'], 0)

    def test_legacy_prompt_and_generated_claim_remain_incomplete_after_recapture(self):
        case, reports, mapping, module, ledger = self.fixture()
        document = case.root / module['document']
        text = document.read_text()
        document.write_text(text.replace('run 对合法输入返回输入加一。', '<!-- agentsmd:draft-claim -->\n待核实：候选行为。'))
        recaptured = evidence.capture(case.root, ledger, reports)
        self.assertTrue(any('unfinished draft placeholder' in e for e in evidence.check(case.root, recaptured, mapping, reports)[0]))
        document.write_text(text + '\n待核实：阅读双方调用位置后补充。\n')
        self.assertTrue(any('unfinished draft' in e for e in modules.check(case.root, mapping, reports, require_reviewed=True)[0]))
        self.assertEqual(completeness.unfinished('## 待核实事项\n外部服务的线上超时尚未确认；本次未访问生产环境。\n'), [])
        self.assertEqual(completeness.unfinished('```markdown\n<!-- agentsmd:placeholder demo -->\n```'), [])

    def test_rerunning_generator_preserves_reviewed_semantic_sections(self):
        case, reports, mapping, module, ledger = self.fixture()
        (case.root / 'AGENTS.md').write_text(f'[src]({module["document"]})\n')
        draft.generate(case.root, mapping, reports, ledger, write=True)
        doc = case.root / 'AGENTS.md'
        text = doc.read_text().replace('<!-- agentsmd:placeholder architecture -->\n待核实：根据关键实现补充流程。\n<!-- agentsmd:endplaceholder -->', '已阅读的架构说明与调用顺序。')
        doc.write_text(text)
        draft.generate(case.root, mapping, reports, ledger, write=True)
        self.assertIn('已阅读的架构说明与调用顺序。', doc.read_text())
        self.assertNotIn('根据关键实现补充流程', doc.read_text())

    def test_schema_errors_pinpoint_nested_fields_and_never_throw_attribute_errors(self):
        case, reports, mapping, module, ledger = self.fixture()
        for field, value, expected in [
            ('capabilities', ['bad'], '.capabilities[0]'),
            ('capabilities', [{'id':'run', 'aspects':{'failure':'bad'}}], '.aspects.failure'),
            ('reviewed_files', 'all', '.reviewed_files'),
            ('source_hashes', {'src/service.py':True}, '.source_hashes'),
            ('exclusions', [{'path':'src/service.py','reason':None}], '.exclusions[0].reason')]:
            with self.subTest(field=field):
                invalid = copy.deepcopy(mapping)
                invalid['modules'][1]['review'][field] = value
                errors, _ = modules.check(case.root, invalid, reports)
                self.assertTrue(any(expected in e for e in errors), errors)
                with self.assertRaises(schemas.SchemaError): draft.generate(case.root, invalid, reports, write=True)
        for value in (None, 'wrong', {}, [None], [{'role':'implementation','path':'src/service.py','line':True}]):
            invalid = copy.deepcopy(ledger);invalid['claims'][0]['evidence'] = value
            errors, _ = evidence.check(case.root, invalid, mapping, reports)
            self.assertTrue(any('.evidence' in e for e in errors))
            with self.assertRaises(schemas.SchemaError): evidence.capture(case.root, invalid, reports)

    def test_invalid_artifact_cli_returns_field_diagnostics_without_tracebacks(self):
        case, reports, mapping, module, ledger = self.fixture()
        invalid = copy.deepcopy(mapping);invalid['modules'][1]['review']['capabilities'] = 'bad'
        self.saved(case, reports, invalid, ledger)
        for script, args in [('verify.py', ['--json']), ('modules.py',['check','--json']), ('evidence.py',['check','--json'])]:
            with self.subTest(script=script):
                result = self.cli(case, script, *args)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                self.assertTrue(any(d['field'].endswith('.review.capabilities') for d in data['diagnostics']))
                self.assertNotIn('Traceback', result.stderr)
        invalid = copy.deepcopy(ledger);invalid['claims'][0]['evidence'][0]['line'] = None
        self.saved(case, reports, mapping, invalid)
        result = self.cli(case, 'evidence.py', 'capture', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertIn('.evidence[0].line', result.stdout)
        self.assertEqual(artifacts.read_json(case.root/'.agentsmd/evidence.json'), invalid)

    def test_top_level_scalars_and_broken_json_never_escape_as_tracebacks(self):
        case, reports, mapping, module, ledger = self.fixture()
        self.saved(case, reports, mapping, ledger)
        map_path = case.root / '.agentsmd/modules.json'
        for raw in ('null', '42', 'false', '[]', '{invalid'):
            with self.subTest(raw=raw):
                map_path.write_text(raw)
                for script, args in [('verify.py', ['--json']), ('modules.py', ['check', '--json']),
                                     ('draft.py', []), ('evidence.py', ['check', '--json'])]:
                    result = self.cli(case, script, *args)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                    if '--json' in args:
                        self.assertGreater(json.loads(result.stdout)['error_count'], 0)

    def test_grouped_output_is_compact_but_json_and_details_are_lossless(self):
        case, reports, mapping, module, ledger = self.fixture()
        warnings = [f"src: unreviewed item {i}" for i in range(250)]
        errors = ['run-success: stale snippet_hash: src/service.py']
        options = Namespace(json=False, details=False, limit=12)
        output = io.StringIO()
        with redirect_stdout(output): result = diagnostics.render(errors, warnings, mapping, ledger, options)
        self.assertLess(len(output.getvalue().splitlines()), 12)
        self.assertEqual(result['warning_count'], 250)
        self.assertEqual(len(result['diagnostics']), 251)
        self.assertEqual(result['groups'][0]['module'], 'src')
        options.details = True
        output = io.StringIO()
        with redirect_stdout(output): diagnostics.render(errors, warnings, mapping, ledger, options)
        self.assertEqual(len(output.getvalue().splitlines()), 251)
        options.json, options.strict = True, True
        output = io.StringIO()
        with redirect_stdout(output): diagnostics.render([], warnings, mapping, ledger, options)
        self.assertEqual(json.loads(output.getvalue())['status'], 'failed')

    def test_explicit_record_review_cli_does_not_touch_other_modules(self):
        case, reports, mapping, module, ledger = self.fixture()
        mapping['modules'][0]['review']['recorded_at'] = 'original marker'
        module['review'].pop('source_hashes')
        self.saved(case, reports, mapping, ledger)
        result = self.cli(case, 'modules.py', 'record-review', '--module', 'src')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = artifacts.read_json(case.root / '.agentsmd/modules.json')
        self.assertEqual(actual['modules'][0], mapping['modules'][0])
        self.assertEqual(set(actual['modules'][1]['review']['source_hashes']), {'src/service.py'})


if __name__ == '__main__':
    unittest.main()
