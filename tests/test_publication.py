"""Regression checks for live scan policy, preview promotion and lexical hints."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifacts
import candidates
import codemask
import codeindex
import draft
import evidence
import guidewrites
import modules
import promotion
import scanpolicy
import schemas
import test_quality
import teststate

REPO = Path(__file__).resolve().parents[1]


class Publication(unittest.TestCase):
    def case(self):
        case = test_quality.Quality(); case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def save(self, case, reports, mapping, ledger):
        for name, value in [('context', reports[0]), ('index', reports[1]), ('modules', mapping), ('evidence', ledger)]:
            artifacts.write_json(case.root / f'.agentsmd/{name}.json', value)

    def preview(self):
        case = self.case()
        reports, mapping, module, ledger = case.fixture(); case.complete(mapping, module)
        self.save(case, reports, mapping, ledger)
        preview, claims = draft.generate(case.root, mapping, reports, ledger)
        captured = evidence.capture(case.root, {'claims': claims}, reports)
        artifacts.write_json(case.root / '.agentsmd/drafts/evidence.draft.json', captured)
        with (case.root / preview['modules'][0]['document']).open('a') as stream:
            stream.write('\nAuthored preview revision.\n')
        return case, reports, mapping, ledger, preview, captured

    def final_bytes(self, case, mapping):
        paths = [m['document'] for m in mapping['modules']] + ['.agentsmd/modules.json', '.agentsmd/evidence.json']
        return {p: (case.root / p).read_bytes() if (case.root / p).exists() else None for p in paths}

    def cli(self, case, script, *args):
        return subprocess.run([sys.executable, str(REPO / 'scripts' / script), '--root', str(case.root), *args],
                              capture_output=True, text=True)

    def test_removing_config_exclusion_invalidates_complete_verification(self):
        case = self.case()
        case.write('extra.py', 'def extra(): return 2\n')
        config = case.write('.agentsmd/config.json', '{"exclude":["extra.py"]}')
        reports, mapping, module, ledger = case.fixture(); case.complete(mapping, module)
        self.save(case, reports, mapping, ledger)
        self.assertEqual(self.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json').returncode, 0)
        for text in ('{"exclude":[]}', '{}'):
            config.write_text(text)
            result = self.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json')
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertTrue(any('selection is stale' in d['message'] for d in json.loads(result.stdout)['diagnostics']))
        config.unlink()
        self.assertNotEqual(scanpolicy.current_selection(case.root, reports[0]['scope'])['scope'], reports[0]['scope'])

    def test_cli_options_are_preserved_separately_from_current_config(self):
        case = self.case()
        case.write('cli.py', 'pass\n'); case.write('config.py', 'pass\n'); case.write('live.py', 'pass\n')
        config = case.write('.agentsmd/config.json', '{"exclude":["config.py"]}')
        old = scanpolicy.select_files(case.root, ['cli.py'], False)
        self.assertEqual(old['scope']['options'], {'exclude': ['cli.py'], 'respect_gitignore': False})
        config.write_text('{"exclude":[],"respect_gitignore":true}')
        current = scanpolicy.current_selection(case.root, old['scope'])
        self.assertNotIn('cli.py', current['files'])
        self.assertIn('config.py', current['files'])
        self.assertFalse(current['scope']['policy']['respect_gitignore'])

    def test_current_gitignore_and_hidden_policy_are_not_overridden_by_old_effective_policy(self):
        case = self.case()
        subprocess.run(['git', 'init', '-q', str(case.root)], check=True)
        case.write('.gitignore', 'ignored.py\n'); case.write('ignored.py', 'pass\n')
        case.write('.custom/hidden.py', 'pass\n')
        config = case.write('.agentsmd/config.json', '{"respect_gitignore":true}')
        old = scanpolicy.select_files(case.root)
        config.write_text('{"respect_gitignore":false,"include_hidden":[".custom"]}')
        current = scanpolicy.current_selection(case.root, old['scope'])
        self.assertIn('ignored.py', current['files'])
        self.assertIn('.custom/hidden.py', current['files'])

    def test_legacy_scopes_require_reanalysis_instead_of_guessing_cli_overrides(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        legacy = copy.deepcopy(reports)
        for report in legacy: del report['scope']['options']
        mapping['scope'] = copy.deepcopy(legacy[0]['scope'])
        self.save(case, legacy, mapping, ledger)
        result = self.cli(case, 'verify.py', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertIn('legacy scan scope', result.stdout)
        with self.assertRaisesRegex(ValueError, 'legacy scan scope'): teststate.snapshot(case.root, legacy)
        malformed = copy.deepcopy(reports[0]); malformed['scope']['options']['respect_gitignore'] = []
        self.assertTrue(any('.options.respect_gitignore' in e for e in schemas.report_errors(malformed)))

    def test_promotion_preserves_authored_text_status_and_source_proofs(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        source_proofs = copy.deepcopy(captured['claims'][0]['evidence'])
        out, promoted = promotion.promote(case.root, preview, captured, reports)
        self.assertEqual([m['document'] for m in out['modules']], [m['document'] for m in mapping['modules']])
        self.assertNotIn('preview', out)
        self.assertIn('Authored preview revision.', (case.root / 'AGENTS.md').read_text())
        for previous, current in zip(preview['modules'], out['modules']):
            self.assertEqual(previous['review'], current['review'])
            self.assertNotIn('final_document', current)
            self.assertNotIn('.agentsmd/drafts', (case.root / current['document']).read_text())
        self.assertEqual(promoted['claims'][0]['evidence'], source_proofs)
        self.assertEqual(promoted['claims'][0]['status'], captured['claims'][0]['status'])
        self.assertEqual(evidence.check(case.root, promoted, out, reports)[0], [])
        result = self.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Repeating an identical promotion is safe; subsequent final edits are conflicts.
        before = self.final_bytes(case, out)
        promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(self.final_bytes(case, out), before)

    def test_write_rejects_preview_maps_and_promote_cli_uses_final_targets(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        before = self.final_bytes(case, mapping)
        result = self.cli(case, 'draft.py', '--map', str(case.root / '.agentsmd/drafts/modules.draft.json'), '--write')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--promote', result.stderr)
        self.assertEqual(before, self.final_bytes(case, mapping))
        result = self.cli(case, 'draft.py', '--promote')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Authored preview revision.', (case.root / 'AGENTS.md').read_text())
        self.assertNotIn('preview', artifacts.read_json(case.root / '.agentsmd/modules.json'))

    def test_final_document_and_artifact_edits_conflict_before_any_promotion_write(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        for target in ('AGENTS.md', '.agentsmd/modules.json', '.agentsmd/evidence.json'):
            path = case.root / target; original = path.read_bytes()
            path.write_bytes(original + b'\n ')
            before = self.final_bytes(case, mapping)
            with self.assertRaisesRegex(ValueError, 'changed since preview'):
                promotion.promote(case.root, preview, captured, reports)
            self.assertEqual(before, self.final_bytes(case, mapping))
            path.write_bytes(original)

    def test_changed_uncaptured_claim_and_stale_sources_cannot_be_rehashed_by_promotion(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        doc = case.root / captured['claims'][0]['document']; original = doc.read_bytes()
        doc.write_text(doc.read_text().replace('run 对合法输入返回输入加一。', 'run 会自动重试。'))
        before = self.final_bytes(case, mapping)
        with self.assertRaisesRegex(schemas.SchemaError, 'changed Markdown'):
            promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(before, self.final_bytes(case, mapping))
        doc.write_bytes(original)
        source = case.root / 'src/service.py'; source.write_text(source.read_text() + '\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'source index stale'):
            promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(before, self.final_bytes(case, mapping))

    def test_promotion_detects_preview_edit_between_validation_and_reading(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        doc = case.root / captured['claims'][0]['document']
        original_check = evidence.check
        def changed(*args, **kwargs):
            result = original_check(*args, **kwargs)
            doc.write_text(doc.read_text() + '\nConcurrent preview edit.\n')
            return result
        before = self.final_bytes(case, mapping)
        with patch.object(evidence, 'check', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'preview input changed'):
                promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(before, self.final_bytes(case, mapping))

    def test_preview_regeneration_preserves_original_baselines_and_authored_text(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        original_hashes = {m['id']: m['final_document_hash'] for m in preview['modules']}
        (case.root / 'AGENTS.md').write_text('Final edited while preview open.\n')
        regenerated, _ = draft.generate(case.root, preview, reports, captured)
        self.assertEqual(original_hashes, {m['id']: m['final_document_hash'] for m in regenerated['modules']})
        self.assertEqual(preview['preview']['artifact_hashes'], regenerated['preview']['artifact_hashes'])
        self.assertIn('Authored preview revision.', (case.root / regenerated['modules'][0]['document']).read_text())
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            promotion.promote(case.root, regenerated, captured, reports)

    def test_promotion_refuses_a_changed_scan_policy_without_touching_final_targets(self):
        case = self.case()
        case.write('extra.py', 'def extra(): return 2\n')
        config = case.write('.agentsmd/config.json', '{"exclude":["extra.py"]}')
        reports, mapping, module, ledger = case.fixture(); case.complete(mapping, module)
        self.save(case, reports, mapping, ledger)
        preview, claims = draft.generate(case.root, mapping, reports, ledger)
        captured = evidence.capture(case.root, {'claims': claims}, reports)
        before = self.final_bytes(case, mapping)
        config.write_text('{"exclude":[]}')
        with self.assertRaisesRegex(ValueError, 'selection is stale'):
            promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(before, self.final_bytes(case, mapping))

    def test_nested_final_documents_and_preview_links_pass_complete_cli_verification(self):
        case = self.case()
        reports, mapping, module, ledger = case.fixture(); case.complete(mapping, module)
        destinations = {'AGENTS.md': 'docs/AGENTS.project.md', module['document']: 'docs/deep folder/AGENTS.feature.md'}
        for entry in mapping['modules']:
            old, final = entry['document'], destinations[entry['document']]
            case.write(final, draft.relocate_links(case.root, (case.root / old).read_text(), old, final, destinations))
            entry['document'] = final
        ledger['claims'][0]['document'] = destinations[ledger['claims'][0]['document']]
        ledger = evidence.capture(case.root, ledger, reports)
        self.save(case, reports, mapping, ledger)
        preview, claims = draft.generate(case.root, mapping, reports, ledger)
        captured = evidence.capture(case.root, {'claims': claims}, reports)
        promotion.promote(case.root, preview, captured, reports)
        result = self.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('(deep%20folder/AGENTS.feature.md)', (case.root / 'docs/AGENTS.project.md').read_text())

    def test_preview_inputs_changed_during_staging_abort_before_final_writes(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        doc = case.root / preview['modules'][0]['document']
        before, original_write, changed = self.final_bytes(case, mapping), guidewrites.replace_bytes, []
        def edit_during_stage(path, content, mode):
            original_write(path, content, mode)
            if path.suffix == '.after' and not changed:
                doc.write_text(doc.read_text() + '\nUser edit during staging.\n'); changed.append(True)
        with patch.object(guidewrites, 'replace_bytes', side_effect=edit_during_stage):
            with self.assertRaisesRegex(ValueError, 'preview input changed while staging'):
                promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(before, self.final_bytes(case, mapping))
        self.assertFalse(guidewrites.pending(case.root))

    def test_bad_final_targets_and_legacy_preview_metadata_are_rejected(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        before = self.final_bytes(case, mapping)
        for target in ('src/service.py', '.agentsmd/drafts/AGENTS.bad.md', 'AGENTS.md'):
            bad = copy.deepcopy(preview); bad['modules'][1]['final_document'] = target
            with self.assertRaises(ValueError): promotion.promote(case.root, bad, captured, reports)
            self.assertEqual(before, self.final_bytes(case, mapping))
        bad = copy.deepcopy(preview); del bad['preview']
        with self.assertRaisesRegex(ValueError, 'baseline metadata'): promotion.promote(case.root, bad, captured, reports)
        bad = copy.deepcopy(preview); del bad['modules'][0]['final_document_hash']
        self.assertTrue(schemas.mapping_errors(bad))

    def test_promotion_rollback_restores_documents_and_final_map_and_ledger(self):
        case, reports, mapping, ledger, preview, captured = self.preview()
        before = self.final_bytes(case, mapping)
        def fail(i, path):
            if path.name == 'evidence.json': raise OSError('injected promotion failure')
        with self.assertRaisesRegex(OSError, 'injected promotion failure'):
            promotion.promote(case.root, preview, captured, reports, after_replace=fail)
        self.assertEqual(before, self.final_bytes(case, mapping))
        self.assertFalse(guidewrites.pending(case.root))

    def test_nested_document_links_queries_and_fenced_examples_are_relocated_correctly(self):
        case = self.case()
        text = ('[child](AGENTS.child.md?mode=full#usage)\n[src](../../src/a.py#L2)\n'
                '```md\n[example](AGENTS.child.md)\n```\n')
        result = draft.relocate_links(case.root, text, '.agentsmd/drafts/AGENTS.md', 'docs/AGENTS.project.md',
            {'.agentsmd/drafts/AGENTS.child.md': 'docs/api/AGENTS.child.md'})
        self.assertIn('[child](api/AGENTS.child.md?mode=full#usage)', result)
        self.assertIn('[src](../src/a.py#L2)', result)
        self.assertIn('```md\n[example](AGENTS.child.md)\n```', result)

    def test_python_literals_comments_and_docstrings_never_supply_failure_candidates(self):
        case = self.case()
        case.write('src/example.py', 'EXAMPLE = """\nraise ValueError("documentation example")\n"""\n'
                   '# throw Error\ndef run(value):\n    """raise RuntimeError()"""\n    return "throw error"\n')
        reports = case.reports(); module = {'files': ['src/example.py']}
        files = artifacts.file_map(reports)
        self.assertEqual(candidates.candidate_refs(case.root, module, files, 'failure'), [])
        self.assertEqual([r['line'] for r in candidates.candidate_refs(case.root, module, files, 'success')], [7])

    def test_go_and_javascript_multiline_literals_and_comments_are_masked(self):
        case = self.case()
        samples = {
            'sample.go': 'package a\nvar example = `\npanic("example")\n`\n/*\nreturn err\n*/\nfunc real() int { return 1 }\n',
            'sample.js': 'const example = `\nthrow new Error("example");\n${`nested throw Error`}\n`;\n/*\nthrow Error("comment")\n*/\nconst pattern = /throw Error/;\nfunction run() { return "ok"; }\n',
        }
        for name, source in samples.items():
            case.write(name, source)
            reports = case.reports()
            if name.endswith('.js') and os.environ.get('AGENTSMD_TYPESCRIPT'):
                reports[1] = codeindex.scan(case.root, js_parser='typescript', typescript=os.environ['AGENTSMD_TYPESCRIPT'])
            files = artifacts.file_map(reports)
            self.assertEqual(candidates.candidate_refs(case.root, {'files': [name]}, files, 'failure'), [], name)
            success = candidates.candidate_refs(case.root, {'files': [name]}, files, 'success')
            if name.endswith('.js') and not os.environ.get('AGENTSMD_TYPESCRIPT'): self.assertEqual(success, [])
            else: self.assertTrue(success, name)
        self.assertIsNone(codemask.code_mask('broken.py', 'x="""\nraise Error()'))
        self.assertIsNone(codemask.code_mask('broken.js', '/* throw Error()'))
        self.assertIsNone(codemask.code_mask('unsupported.rb', 'raise Error'))


if __name__ == '__main__': unittest.main()
