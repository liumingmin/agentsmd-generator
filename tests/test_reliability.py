"""Behavioral checks for malformed data, useful guides, test provenance and recovery."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts')]
import artifacts
import candidates
import completeness
import diagnostics
import draft
import guidewrites
import modules
import reviewstate
import schemas
import test_quality
import teststate

REPO = Path(__file__).resolve().parents[1]


class Reliability(unittest.TestCase):
    def fixture(self):
        case = test_quality.Quality()
        case.setUp()
        self.addCleanup(case.doCleanups)
        reports, mapping, module, ledger = case.fixture()
        case.complete(mapping, module)
        return case, reports, mapping, module, ledger

    def save(self, case, reports, mapping, ledger):
        for name, value in [('context', reports[0]), ('index', reports[1]), ('modules', mapping), ('evidence', ledger)]:
            artifacts.write_json(case.root / f'.agentsmd/{name}.json', value)

    def cli(self, case, script, *args):
        return subprocess.run([sys.executable, str(REPO / 'scripts' / script), '--root', str(case.root), *args],
                              capture_output=True, text=True)

    def test_invalid_grouping_ids_cannot_crash_diagnostics_or_cli(self):
        case, reports, mapping, module, ledger = self.fixture()
        for invalid in (['invalid'], {'invalid': True}, 42, True, None):
            bad = copy.deepcopy(mapping)
            bad['modules'][1]['id'] = invalid
            with redirect_stdout(io.StringIO()):
                data = diagnostics.render(schemas.mapping_errors(bad), [], mapping=bad)
            self.assertEqual(data['diagnostics'][0]['field'], '$.modules[1].id')
            self.save(case, reports, bad, ledger)
            for script, args in [('modules.py', ['check', '--json']), ('verify.py', ['--json']),
                                 ('evidence.py', ['check', '--json'])]:
                result = self.cli(case, script, *args)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertGreater(json.loads(result.stdout)['error_count'], 0)
        bad_ledger = copy.deepcopy(ledger)
        bad_ledger['claims'][0]['module'] = ['invalid']
        data = diagnostics.summary(schemas.ledger_errors(bad_ledger), [], ledger=bad_ledger)
        self.assertIsNone(data['diagnostics'][0]['module'])

    def test_report_hash_identity_and_policy_types_fail_before_loading(self):
        case, reports, mapping, module, ledger = self.fixture()
        self.save(case, reports, mapping, ledger)
        for field, value in [('selection_hash', []), ('policy', []), ('gitignore_applied', 'yes')]:
            bad = copy.deepcopy(reports[1])
            bad['scope'][field] = value
            artifacts.write_json(case.root / '.agentsmd/index.json', bad)
            with self.assertRaises(schemas.SchemaError): artifacts.load_reports(case.root)
            result = self.cli(case, 'verify.py', '--json')
            self.assertEqual(result.returncode, 1)
            self.assertIn('.scope.' + field, result.stdout)
            self.assertTrue(any(d['field'] == '$.scope.' + field for d in json.loads(result.stdout)['diagnostics']))
            self.assertNotIn('Traceback', result.stderr)
        bad = copy.deepcopy(reports[1]); bad['snapshot_id'] = ['bad']
        self.assertTrue(any('snapshot_id' in e for e in schemas.report_errors(bad)))

    def test_navigation_only_and_empty_headings_cannot_pass_complete_delivery(self):
        case, reports, mapping, module, ledger = self.fixture()
        root_doc = case.root / 'AGENTS.md'
        for content in (f'[src]({module["document"]})\n',
                        f'[src]({module["document"]})\n## 项目定位\n## 架构与数据流\n## 构建/运行/测试\n',
                        f'[src]({module["document"]})\n## 项目定位\nTODO\n## 架构与数据流\n| 函数 | 文件 |\n## 构建/运行/测试\n[service](src/service.py)\n'):
            root_doc.write_text(content)
            errors, _ = modules.check(case.root, mapping, reports, require_reviewed=True)
            self.assertEqual(sum('document section' in e for e in errors), 3)
        self.save(case, reports, mapping, ledger)
        result = self.cli(case, 'verify.py', '--delivery', 'complete', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(d['code'] == 'document.incomplete' for d in json.loads(result.stdout)['diagnostics']))

    def test_custom_nested_and_english_sections_can_pass_without_fixed_titles(self):
        case, reports, mapping, module, ledger = self.fixture()
        (case.root / 'AGENTS.md').write_text(f'[src]({module["document"]})\n' +
            '## 使用场景\n<!-- agentsmd:section purpose -->\n面向本地计算调用者。\n' +
            '## 内部设计\n<!-- agentsmd:section architecture -->\n### 参数流\n校验参数后返回计算结果。\n' +
            '## 开始使用\n<!-- agentsmd:section commands -->\n无构建步骤；使用 Python 调用，目标测试未运行。\n')
        # Regeneration must recognize custom sections and avoid adding scaffolding.
        draft.generate(case.root, mapping, reports, ledger, write=True)
        self.assertNotIn('agentsmd:placeholder', (case.root / 'AGENTS.md').read_text())
        self.save(case, reports, mapping, ledger)
        result = self.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(completeness.section_errors('## Overview\nA local calculator.\n## Architecture\nCalls a pure function.\n## Getting started\nRun Python; no build is needed.\n', root=True), [])

    def test_generated_navigation_and_examples_are_not_explanations(self):
        generated = '<!-- agentsmd:draft-navigation -->\n## 功能点清单\nAutomatically generated statistics.\n<!-- agentsmd:enddraft-navigation -->\n'
        example = '```markdown\n## 职责与目录\nSome prose.\n## 依赖与协作\nSome prose.\n## 测试与验证\nSome prose.\n```'
        self.assertEqual(len(completeness.section_errors(generated + example)), 4)

    def test_reading_candidates_follow_aspects_instead_of_first_symbol(self):
        case, reports, mapping, module, ledger = self.fixture()
        case.write('src/aaa.py', 'CONSTANT = 1\n')
        case.write('src/service.py', 'import os\ndef run(value):\n    assert value is not None\n    limit = os.getenv("LIMIT")\n    if value < 0:\n        raise ValueError("negative")\n    return value + 1\n')
        reports = case.reports()
        module['files'].insert(0, 'src/aaa.py')
        files = artifacts.file_map(reports)
        expected = {'input': 3, 'configuration': 4, 'failure': 6, 'success': 7}
        for aspect, line in expected.items():
            refs = candidates.candidate_refs(case.root, module, files, aspect, {'id': 'run', 'title': 'Run'})
            self.assertTrue(any(r['path'] == 'src/service.py' and r['line'] == line for r in refs), (aspect, refs))
            self.assertFalse(any(r['path'] == 'src/aaa.py' for r in refs))

    def test_missing_candidates_are_explicit_and_never_auto_confirmed(self):
        case, reports, mapping, module, ledger = self.fixture()
        module['review']['capabilities'] = []
        self.assertEqual(candidates.candidate_refs(case.root, module, artifacts.file_map(reports), 'configuration'), [])
        preview, claims = draft.generate(case.root, mapping, reports)
        config = next(c for c in claims if c['aspect'] == 'configuration')
        self.assertEqual(config['status'], 'unknown')
        self.assertEqual(config['evidence'], [])
        self.assertIn('未找到该方面候选证据', (case.root / config['document']).read_text())

    def test_test_results_expire_after_source_config_or_new_input_changes(self):
        case, reports, mapping, module, ledger = self.fixture()
        case.write('settings.json', '{"limit":1}')
        reports = case.reports(); mapping['scope'] = reports[0]['scope']
        stamped, status = teststate.run(case.root, mapping, reports, ['src'], [sys.executable, '-c', 'assert 2 + 2 == 4'])
        self.assertEqual(status, 0)
        execution = stamped['modules'][1]['review']['test']['execution']
        self.assertEqual(teststate.freshness(execution, teststate.snapshot(case.root, reports)), [])
        for relative, text in [('src/service.py', '\n# modified\n'), ('settings.json', '\n '), ('new.py', 'VALUE=1\n')]:
            path = case.root / relative
            before = path.read_bytes() if path.exists() else None
            path.write_text((before.decode() if before else '') + text)
            try:
                refreshed = case.reports()
                updated = copy.deepcopy(stamped); updated['scope'] = refreshed[0]['scope']
                updated = reviewstate.record(case.root, updated, refreshed, ['src'])
                errors, _ = modules.check(case.root, updated, refreshed, require_reviewed=True)
                self.assertTrue(any('test execution stale' in e for e in errors), errors)
                self.assertEqual(updated['modules'][1]['review']['test']['execution'], execution)
            finally:
                if before is None: path.unlink()
                else: path.write_bytes(before)
        with (case.root / 'AGENTS.md').open('a') as stream: stream.write('\nGuide-only edit.\n')
        self.assertEqual(teststate.freshness(execution, teststate.snapshot(case.root, reports)), [])

    def test_historical_test_metadata_is_warning_in_draft_and_error_when_complete(self):
        case, reports, mapping, module, ledger = self.fixture()
        module['review']['test'] = {'status': 'passed', 'reason': 'Historical run', 'execution': {
            'command': 'old-test', 'cwd': '.', 'timestamp': '2026-10-21T01:00:00Z', 'exit_code': 0}}
        errors, _ = modules.check(case.root, mapping, reports)
        self.assertTrue(any('test execution snapshot missing' in e for e in errors))
        module['review']['scope'] = 'unreviewed'
        errors, warnings = modules.check(case.root, mapping, reports)
        self.assertEqual(errors, [])
        self.assertTrue(any('test execution snapshot missing' in w for w in warnings))

    def test_test_command_cli_records_actual_failure_and_success_without_promotion(self):
        case, reports, mapping, module, ledger = self.fixture()
        module['review']['scope'] = 'sampled'
        self.save(case, reports, mapping, ledger)
        for code, status, exit_code in [('raise SystemExit(3)', 'failed', 1), ('assert True', 'passed', 0)]:
            result = self.cli(case, 'teststate.py', '--module', 'src', '--', sys.executable, '-c', code)
            self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
            actual = artifacts.read_json(case.root / '.agentsmd/modules.json')['modules'][1]['review']
            self.assertEqual(actual['scope'], 'sampled')
            self.assertEqual(actual['capabilities'], module['review']['capabilities'])
            self.assertEqual(actual['test']['status'], status)
            self.assertEqual(actual['test']['execution']['argv'], [sys.executable, '-c', code])
            self.assertIn('src/service.py', actual['test']['execution']['source_hashes'])

    def test_test_that_mutates_its_inputs_is_never_current(self):
        case, reports, mapping, module, ledger = self.fixture()
        updated, status = teststate.run(case.root, mapping, reports, ['src'], [sys.executable, '-c',
            'from pathlib import Path; p=Path("src/service.py"); p.write_text(p.read_text()+"\\n# changed by test\\n")'])
        self.assertEqual(status, 0)
        execution = updated['modules'][1]['review']['test']['execution']
        self.assertFalse(execution['inputs_unchanged'])
        self.assertTrue(teststate.freshness(execution, teststate.snapshot(case.root, case.reports())))

    def test_scan_config_content_is_bound_even_when_selection_is_unchanged(self):
        case, reports, mapping, module, ledger = self.fixture()
        config = case.write('.agentsmd/config.json', '{"exclude":[]}')
        reports = case.reports(); mapping['scope'] = reports[0]['scope']
        output, _ = teststate.run(case.root, mapping, reports, ['src'], [sys.executable, '-c', 'assert True'])
        execution = output['modules'][1]['review']['test']['execution']
        self.assertIn('.agentsmd/config.json', execution['source_hashes'])
        config.write_text('{"exclude":[], "note":"policy review"}')
        current = teststate.snapshot(case.root, reports)
        self.assertEqual(current['scope'], execution['scope'])
        self.assertTrue(teststate.freshness(execution, current))

    def test_stale_analysis_refuses_to_launch_a_command(self):
        case, reports, mapping, module, ledger = self.fixture()
        with (case.root / 'src/service.py').open('a') as stream: stream.write('\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'test source index stale'):
            teststate.run(case.root, mapping, reports, ['src'], [sys.executable, '-c',
                'from pathlib import Path; Path("command-started").touch()'])
        self.assertFalse((case.root / 'command-started').exists())

    def test_batch_ordinary_failure_restores_all_documents_and_companion_artifacts(self):
        case, reports, mapping, module, ledger = self.fixture()
        paths = ['AGENTS.md', module['document'], '.agentsmd/evidence.draft.json']
        case.write(paths[-1], '{"old":true}')
        before = {p: (case.root / p).read_bytes() for p in paths}
        writes = {p: b'changed\n' for p in paths}; writes['.agentsmd/modules.draft.json'] = b'{}\n'
        def fail(i, path):
            if i == 2: raise OSError('injected replacement failure')
        with self.assertRaisesRegex(OSError, 'injected'):
            guidewrites.apply(case.root, writes, after_replace=fail)
        self.assertEqual({p: (case.root / p).read_bytes() for p in paths}, before)
        self.assertFalse((case.root / '.agentsmd/modules.draft.json').exists())
        self.assertFalse(guidewrites.pending(case.root))

    def interrupted(self, case, module):
        paths = ['AGENTS.md', module['document'], '.agentsmd/modules.draft.json']
        before = {p: ((case.root / p).read_bytes() if (case.root / p).exists() else None) for p in paths}
        code = ('import os,sys;sys.path.insert(0,sys.argv[1]);import guidewrites;'
                'guidewrites.apply(sys.argv[2], {p:b"changed\\n" for p in sys.argv[3:]},'
                'after_replace=lambda i,p:os._exit(97) if i==1 else None)')
        result = subprocess.run([sys.executable, '-c', code, str(REPO / 'scripts'), str(case.root), *paths])
        self.assertEqual(result.returncode, 97)
        self.assertTrue(guidewrites.pending(case.root))
        return before

    def test_killed_batch_blocks_delivery_and_cli_recovery_is_idempotent(self):
        case, reports, mapping, module, ledger = self.fixture()
        self.save(case, reports, mapping, ledger)
        before = self.interrupted(case, module)
        result = self.cli(case, 'verify.py', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertIn('unfinished guide batch', result.stdout)
        with self.assertRaisesRegex(ValueError, 'unfinished guide batch'):
            draft.generate(case.root, mapping, reports)
        with self.assertRaisesRegex(ValueError, 'unfinished guide batch'):
            teststate.run(case.root, mapping, reports, ['src'], [sys.executable, '-c', 'raise AssertionError("must not launch")'])
        for _ in range(2):
            result = self.cli(case, 'draft.py', '--recover')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path, content in before.items():
            self.assertEqual((case.root / path).read_bytes() if (case.root / path).exists() else None, content)

    def test_recovery_conflicts_and_corrupt_backups_never_overwrite_user_edits(self):
        case, reports, mapping, module, ledger = self.fixture()
        before = self.interrupted(case, module)
        doc = case.root / 'AGENTS.md'
        doc.write_text('User edit after interruption.\n')
        with self.assertRaisesRegex(ValueError, 'recovery conflict'): guidewrites.recover(case.root)
        self.assertEqual(doc.read_text(), 'User edit after interruption.\n')
        self.assertEqual((case.root / module['document']).read_bytes(), b'changed\n')
        doc.write_bytes(b'changed\n')
        journal = artifacts.read_json(guidewrites.store(case.root) / 'active.json')
        backup = guidewrites.store(case.root) / journal['id'] / '0.before'
        original = backup.read_bytes(); backup.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'): guidewrites.recover(case.root)
        self.assertEqual(doc.read_bytes(), b'changed\n')
        backup.write_bytes(original)
        self.assertTrue(guidewrites.recover(case.root))
        self.assertEqual(doc.read_bytes(), before['AGENTS.md'])

    def test_committed_batch_recovery_keeps_outputs_and_file_permissions(self):
        case, reports, mapping, module, ledger = self.fixture()
        doc = case.root / 'AGENTS.md'; doc.chmod(0o640)
        with patch.object(guidewrites, 'finish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): guidewrites.apply(case.root, {'AGENTS.md': b'Committed.\n'})
        self.assertTrue(guidewrites.recover(case.root))
        self.assertEqual(doc.read_bytes(), b'Committed.\n')
        self.assertEqual(doc.stat().st_mode & 0o777, 0o640)
        self.assertFalse(guidewrites.pending(case.root))


if __name__ == '__main__':
    unittest.main()
