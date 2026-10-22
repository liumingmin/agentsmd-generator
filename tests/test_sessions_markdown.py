"""Observable regressions for continuing previews, Markdown and JSX hints."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifacts
import candidates
import codeindex
import codemask
import draft
import diff
import evidence
import guidewrites
import markdownlinks
import modules
import promotion
import test_publication

TYPESCRIPT = os.environ.get('AGENTSMD_TYPESCRIPT')


class SessionsMarkdown(unittest.TestCase):
    def preview(self):
        helper = test_publication.Publication()
        self.addCleanup(helper.doCleanups)
        return (helper,) + helper.preview()

    def test_default_generation_resumes_saved_baseline_and_rejects_final_edit_on_promotion(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        before_hash = preview['modules'][0]['final_document_hash']
        final = case.root / 'AGENTS.md'
        final.write_text(final.read_text() + '\nFinal edit after preview.\n')
        result = helper.cli(case, 'draft.py')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = artifacts.read_json(case.root / '.agentsmd/drafts/modules.draft.json')
        proofs = artifacts.read_json(case.root / '.agentsmd/drafts/evidence.draft.json')
        self.assertEqual(saved['modules'][0]['final_document_hash'], before_hash)
        self.assertEqual(saved['preview']['artifact_hashes'], preview['preview']['artifact_hashes'])
        self.assertEqual(proofs['claims'], captured['claims'])
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            promotion.promote(case.root, saved, proofs, reports)
        self.assertIn('Final edit after preview.', final.read_text())

    def test_default_resume_preserves_preview_review_and_evidence_instead_of_final_records(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        preview['modules'][1]['review']['test']['reason'] = 'Preview-specific reading disposition.'
        artifacts.write_json(case.root / '.agentsmd/drafts/modules.draft.json', preview)
        resumed, claims = draft.generate(case.root, mapping, reports, ledger)
        self.assertEqual(resumed['modules'][1]['review'], preview['modules'][1]['review'])
        self.assertEqual(claims, captured['claims'])
        self.assertIn('Authored preview revision.', (case.root / resumed['modules'][0]['document']).read_text())

    def test_new_preview_uses_empty_directory_and_current_final_content_without_resetting_old_session(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        path = case.root / '.agentsmd/drafts/modules.draft.json'; old = path.read_bytes()
        final = case.root / 'AGENTS.md'; final.write_text(final.read_text() + '\nCurrent final revision.\n')
        rejected = helper.cli(case, 'draft.py', '--new-preview')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(path.read_bytes(), old)
        result = helper.cli(case, 'draft.py', '--new-preview', '--out-dir', '.agentsmd/drafts/session-b')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        new = artifacts.read_json(case.root / '.agentsmd/drafts/session-b/modules.draft.json')
        self.assertIn('Current final revision.', (case.root / new['modules'][0]['document']).read_text())
        self.assertNotEqual(new['modules'][0]['final_document_hash'], preview['modules'][0]['final_document_hash'])
        self.assertEqual(path.read_bytes(), old)
        result = helper.cli(case, 'draft.py', '--map', str(case.root / '.agentsmd/drafts/session-b/modules.draft.json'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(path.read_bytes(), old)

    def test_missing_or_corrupt_session_metadata_never_rebases_existing_preview(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        mp = case.root / '.agentsmd/drafts/modules.draft.json'
        ep = case.root / '.agentsmd/drafts/evidence.draft.json'
        old_map, old_ledger = mp.read_bytes(), ep.read_bytes()
        doc = case.root / preview['modules'][0]['document']; old_doc = doc.read_bytes()
        for action in ('missing-map', 'missing-ledger', 'corrupt-map'):
            mp.write_bytes(old_map); ep.write_bytes(old_ledger)
            if action == 'missing-map': mp.unlink()
            elif action == 'missing-ledger': ep.unlink()
            else: mp.write_text('null')
            with self.assertRaises(ValueError): draft.generate(case.root, mapping, reports, ledger)
            self.assertEqual(doc.read_bytes(), old_doc)

    def test_incompatible_layout_and_rebased_input_metadata_are_rejected(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        changed = copy.deepcopy(mapping); changed['modules'][1]['document'] = 'AGENTS.renamed.md'
        with self.assertRaisesRegex(ValueError, 'layout/scope changed'):
            draft.generate(case.root, changed, reports)
        changed = copy.deepcopy(preview); changed['modules'][0]['final_document_hash'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'baseline changed'):
            draft.generate(case.root, changed, reports, captured)

    def test_empty_directories_left_by_recovery_do_not_block_new_generation(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        (case.root / '.agentsmd/drafts/recovered/empty/nested').mkdir(parents=True)
        generated, _ = draft.generate(case.root, mapping, reports, ledger, out_dir='.agentsmd/drafts/recovered')
        self.assertEqual(generated['preview']['directory'], '.agentsmd/drafts/recovered')

    def test_session_metadata_edit_during_generation_is_not_overwritten(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        mp = case.root / '.agentsmd/drafts/modules.draft.json'
        doc = case.root / preview['modules'][0]['document']; old_doc = doc.read_bytes()
        old_replace, edited = draft.replace_managed, []
        def concurrent(text, name, content):
            if not edited:
                value = artifacts.read_json(mp); value['session_note'] = 'Concurrent edit.'
                artifacts.write_json(mp, value); edited.append(True)
            return old_replace(text, name, content)
        with patch.object(draft, 'replace_managed', side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, 'preview input changed'):
                draft.generate(case.root, mapping, reports, ledger)
        self.assertEqual(doc.read_bytes(), old_doc)
        self.assertEqual(artifacts.read_json(mp)['session_note'], 'Concurrent edit.')

    def test_relocation_changes_only_destinations_and_preserves_code_examples(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        source = ('[AGENTS.md](AGENTS.md "AGENTS.md")\n'
                  '`[guide](AGENTS.md)`\n``[guide](AGENTS.md) ` nested``\n'
                  '```md\n[guide](AGENTS.md)\n```\n'
                  '> ~~~md\n> [guide](AGENTS.md)\n> ~~~\n'
                  '    [guide](AGENTS.md)\n\\[guide](AGENTS.md)\n'
                  '<!-- [guide](AGENTS.md) -->\n')
        result = draft.relocate_links(case.root, source, 'AGENTS.example.md', 'docs/AGENTS.example.md')
        self.assertEqual(result, source.replace('[AGENTS.md](AGENTS.md "AGENTS.md")', '[AGENTS.md](../AGENTS.md "AGENTS.md")'))

    def test_reference_forms_images_titles_queries_and_nested_parentheses_are_preserved(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        source = ('[visible][Guide ID] [Guide ID][] [Guide ID]\n'
                  '[Guide ID]: <AGENTS.md?mode=full#usage> "Visible title"\n'
                  '[unused]: do-not-touch.md\n'
                  '[![icon](img/a(b).png "icon")](AGENTS.md)\n')
        result = draft.relocate_links(case.root, source, 'AGENTS.example.md', 'docs/AGENTS.example.md')
        self.assertIn('[visible][Guide ID] [Guide ID][] [Guide ID]\n', result)
        self.assertIn('[Guide ID]: <../AGENTS.md?mode=full#usage> "Visible title"', result)
        self.assertIn('[unused]: do-not-touch.md', result)
        self.assertIn('[![icon](../img/a%28b%29.png "icon")](../AGENTS.md)', result)
        self.assertEqual(len(markdownlinks.links(source)), 5)

    def test_reference_navigation_and_source_evidence_outside_claim_pass_promotion(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        claim = captured['claims'][0]; doc = case.root / claim['document']
        link = next(x for x in markdownlinks.links(doc.read_text()) if x.label.startswith('src/service.py:L'))
        text = doc.read_text()
        text = text[:link.start] + '[' + link.label + '][source]' + text[link.end:]
        doc.write_text(text + '\n[source]: ' + link.target + ' "Source evidence"\n')
        root_doc = case.root / preview['modules'][0]['document']
        child = Path(preview['modules'][1]['document']).name
        with root_doc.open('a') as stream: stream.write('\n[Detailed module][child-guide]\n\n[child-guide]: ' + child + '\n')
        captured = evidence.capture(case.root, captured, reports)
        self.assertEqual(evidence.check(case.root, captured, preview, reports)[0], [])
        out, promoted = promotion.promote(case.root, preview, captured, reports)
        final_doc = case.root / promoted['claims'][0]['document']
        self.assertIn('[src/service.py:L1-L3][source]', final_doc.read_text())
        self.assertIn('[source]: src/service.py#L1-L3 "Source evidence"', final_doc.read_text())
        self.assertEqual(promoted['claims'][0]['evidence'], claim['evidence'])
        self.assertEqual(evidence.check(case.root, promoted, out, reports)[0], [])
        result = helper.cli(case, 'verify.py', '--require-reviewed', '--strict', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_changed_reference_definition_cannot_bypass_evidence_or_dead_link_checks(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        doc = case.root / captured['claims'][0]['document']; text = doc.read_text()
        link = next(x for x in markdownlinks.links(text) if x.label.startswith('src/service.py:L'))
        doc.write_text(text[:link.start] + '[' + link.label + '][source]' + text[link.end:] + '\n[source]: ' + link.target + '\n')
        captured = evidence.capture(case.root, captured, reports)
        original_hash = captured['claims'][0]['statement_hash']
        doc.write_text(doc.read_text().replace('[source]: ' + link.target, '[source]: missing.py#L1'))
        self.assertEqual(evidence.blocks(case.root, captured['claims'][0]['document'])['run-success'], captured['claims'][0]['statement'])
        errors, _ = evidence.check(case.root, captured, preview, reports)
        self.assertTrue(any('mismatch' in e or 'anchor' in e for e in errors), errors)
        self.assertEqual(original_hash, captured['claims'][0]['statement_hash'])

    def test_code_example_source_labels_are_not_live_evidence(self):
        from citations import validate_citations
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        statement = '`[missing.py:L99](missing.py#L99)`\n'
        self.assertEqual(validate_citations(case.root, 'AGENTS.md', statement, []), [])
        self.assertTrue(validate_citations(case.root, 'AGENTS.md', statement, [{'path': 'src/service.py', 'line': 1}]))
        self.assertEqual(markdownlinks.links('\\[escaped](missing.py)\n'), [])

    def test_incremental_doc_impact_resolves_references_and_ignores_code_examples(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        case.write('AGENTS.probe.md', '`[src/service.py:L1](src/service.py#L1)`\n[Service][src]\n\n[src]: src/service.py#L1\n')
        affected = diff.affected_docs(case.root, {'src/service.py'})
        self.assertEqual(affected['AGENTS.probe.md'], [(2, 'src/service.py')])

    @unittest.skipUnless(TYPESCRIPT, 'real TypeScript compiler required')
    def test_jsx_ast_excludes_display_text_attributes_and_handlers_but_keeps_outer_code(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        source = ('const emoji = "😀";\nexport function View(input) {\n'
                  ' if (!input) throw new Error("real failure");\n'
                  ' return <div>\nthrow new Error("display text")\n'
                  '<button title="throw Error" onClick={() => { throw Error("handler"); }}>raise Error</button>\n'
                  '</div>;\n}\n')
        case.write('view.tsx', source)
        report = codeindex.scan(case.root, js_parser='typescript', typescript=TYPESCRIPT)
        file = next(f for f in report['files'] if f['path'] == 'view.tsx')
        self.assertTrue(file['candidate_syntax_valid'])
        self.assertEqual(len(file['jsx_ranges']), 1)
        masked = codemask.code_mask('view.tsx', source, file['candidate_exclusions'])
        self.assertEqual(len(masked.splitlines()), len(source.splitlines()))
        self.assertNotIn('display text', masked)
        files = artifacts.file_map([report]); module = {'files': ['view.tsx']}
        self.assertEqual([r['line'] for r in candidates.candidate_refs(case.root, module, files, 'failure')], [3])
        self.assertEqual([r['line'] for r in candidates.candidate_refs(case.root, module, files, 'success')], [4])

    @unittest.skipUnless(TYPESCRIPT, 'real TypeScript compiler required')
    def test_jsx_fragment_same_line_and_javascript_extension_have_no_text_failures(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        for name in ('view.jsx', 'view.tsx', 'view.js'):
            case.write(name, 'export function View() { return <>throw new Error()<span>panic()</span></>; }\n')
        report = codeindex.scan(case.root, js_parser='typescript', typescript=TYPESCRIPT)
        self.assertEqual(report['diagnostics'], [])
        files = artifacts.file_map([report])
        for name in ('view.jsx', 'view.tsx', 'view.js'):
            self.assertEqual(candidates.candidate_refs(case.root, {'files': [name]}, files, 'failure'), [])
            self.assertTrue(candidates.candidate_refs(case.root, {'files': [name]}, files, 'success'))

    def test_jsx_without_reliable_ast_or_invalid_ranges_produces_no_candidates(self):
        helper, case, reports, mapping, ledger, preview, captured = self.preview()
        source = 'export function View() { return <div>throw Error()</div>; }\n'
        case.write('view.tsx', source)
        report = codeindex.scan(case.root, js_parser='regex')
        self.assertEqual(candidates.candidate_refs(case.root, {'files': ['view.tsx']}, artifacts.file_map([report]), 'failure'), [])
        self.assertIsNone(codemask.code_mask('view.tsx', source))
        self.assertIsNone(codemask.code_mask('view.tsx', source, [{'start': True, 'end': 3}]))
        self.assertIsNone(codemask.code_mask('view.tsx', source, [], syntax_valid=False))


if __name__ == '__main__': unittest.main()
