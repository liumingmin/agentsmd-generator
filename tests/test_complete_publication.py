"""Complete publication cannot silently accept sampled module reading."""
import copy
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifacts
import completeness
import draft
import evidence
import modules
import promotion
import test_publication


class CompletePublication(unittest.TestCase):
    def helper(self):
        helper = test_publication.Publication()
        self.addCleanup(helper.doCleanups)
        return helper

    def sampled(self, helper):
        case = helper.case()
        case.write('src/other.py', 'def other(): return 2\n')
        reports, mapping, module, ledger = case.fixture()
        case.complete(mapping, module)
        mapping['delivery']['mode'] = 'sampled'
        module['review']['scope'] = 'sampled'
        module['review']['reviewed_files'] = ['src/service.py']
        module['review']['capabilities'][0]['status'] = 'pending'
        doc = case.root / module['document']
        doc.write_text(doc.read_text().replace('run 对合法输入返回输入加一。', '待核实：run 的结果尚未完成核对。'))
        ledger['claims'][0].update(status='unknown', reason='Partial reading in progress')
        ledger = evidence.capture(case.root, ledger, reports)
        helper.save(case, reports, mapping, ledger)
        preview, claims = draft.generate(case.root, mapping, reports, ledger)
        captured = evidence.capture(case.root, {'claims': claims}, reports)
        artifacts.write_json(case.root / '.agentsmd/drafts/evidence.draft.json', captured)
        return case, reports, mapping, preview, captured

    def test_sampled_preview_is_rejected_by_default_before_any_final_write(self):
        helper = self.helper()
        case, reports, mapping, preview, captured = self.sampled(helper)
        before = helper.final_bytes(case, mapping)
        with self.assertRaisesRegex(ValueError, 'requires delivery.mode=complete'):
            promotion.promote(case.root, preview, captured, reports)
        result = helper.cli(case, 'draft.py', '--promote')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires delivery.mode=complete', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(helper.final_bytes(case, mapping), before)

    def test_explicit_sampled_publication_preserves_partial_reading_and_unknown_claims(self):
        helper = self.helper()
        case, reports, mapping, preview, captured = self.sampled(helper)
        result = helper.cli(case, 'draft.py', '--promote', '--delivery', 'sampled')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = artifacts.read_json(case.root / '.agentsmd/modules.json')
        ledger = artifacts.read_json(case.root / '.agentsmd/evidence.json')
        self.assertEqual([m['review'] for m in out['modules']], [m['review'] for m in preview['modules']])
        self.assertEqual(out['delivery']['mode'], 'sampled')
        self.assertEqual(ledger['claims'][0]['status'], 'unknown')
        module = next(m for m in out['modules'] if m['files'])
        self.assertNotIn('src/other.py', module['review']['reviewed_files'])
        self.assertIn('抽样审阅', (case.root / module['document']).read_text())
        self.assertEqual(helper.cli(case, 'verify.py', '--delivery', 'sampled', '--json').returncode, 0)
        self.assertEqual(helper.cli(case, 'verify.py', '--delivery', 'complete', '--json').returncode, 1)

    def test_changing_delivery_flag_or_json_cannot_bypass_incomplete_reading_and_evidence(self):
        helper = self.helper()
        case, reports, mapping, preview, captured = self.sampled(helper)
        before = helper.final_bytes(case, mapping)
        relabeled = copy.deepcopy(preview)
        relabeled['delivery']['mode'] = 'complete'
        for change_scope in (False, True):
            if change_scope:
                for module in relabeled['modules']:
                    if module['files']: module['review']['scope'] = 'complete'
            with self.assertRaises(ValueError) as caught:
                promotion.promote(case.root, relabeled, captured, reports)
            self.assertIn('unconfirmed capability claim', str(caught.exception))
            self.assertIn('every owned source', str(caught.exception))
            self.assertEqual(helper.final_bytes(case, mapping), before)
        with self.assertRaisesRegex(ValueError, 'requires delivery.mode=sampled'):
            promotion.promote(case.root, relabeled, captured, reports, delivery='sampled')
        artifacts.write_json(case.root / '.agentsmd/drafts/modules.draft.json', relabeled)
        result = helper.cli(case, 'draft.py', '--promote', '--delivery', 'complete')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('every owned source', result.stderr)
        self.assertEqual(helper.final_bytes(case, mapping), before)

    def test_completed_preview_requires_refresh_of_sampled_navigation_and_keeps_authored_features(self):
        helper = self.helper()
        case, reports, mapping, ledger, preview, captured = helper.preview()
        module = next(m for m in preview['modules'] if m['files'])
        doc = case.root / module['document']
        authored = doc.read_text().split('<!-- agentsmd:enddraft-navigation -->')[1]
        doc.write_text(doc.read_text().replace('已登记完整审阅范围；仍须运行交付校验。', '抽样审阅，未完成整个模块。'))
        before = helper.final_bytes(case, mapping)
        with self.assertRaisesRegex(ValueError, 'generated review status is stale'):
            promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(helper.final_bytes(case, mapping), before)
        # Updating the current preview, rather than the final map, refreshes status.
        result = helper.cli(case, 'draft.py', '--map', str(case.root / '.agentsmd/drafts/modules.draft.json'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(doc.read_text().split('<!-- agentsmd:enddraft-navigation -->')[1], authored)
        self.assertNotIn('抽样审阅，未完成整个模块。', doc.read_text())
        self.assertEqual(modules.check(case.root, preview, reports, require_reviewed=True)[0], [])
        self.assertEqual(helper.cli(case, 'draft.py', '--promote').returncode, 0)
        self.assertEqual(helper.cli(case, 'verify.py', '--delivery', 'complete', '--strict', '--json').returncode, 0)

    def test_status_statistics_and_real_feature_section_are_separate_even_for_legacy_navigation(self):
        helper = self.helper(); case = helper.case()
        reports, mapping, module, ledger = case.fixture()
        module['review']['scope'] = 'sampled'
        # An old managed feature heading contained only status and measured counts.
        doc = case.root / module['document']
        doc.write_text(doc.read_text() + '\n<!-- agentsmd:draft-navigation -->\n## 功能点清单\n抽样审阅，未完成整个模块。\n<!-- agentsmd:enddraft-navigation -->\n')
        preview, claims = draft.generate(case.root, mapping, reports, ledger)
        generated = next(m for m in preview['modules'] if m['id'] == module['id'])
        text = (case.root / generated['document']).read_text()
        navigation = re.search(r'<!-- agentsmd:draft-navigation -->(.*?)<!-- agentsmd:enddraft-navigation -->', text, re.S)[1]
        self.assertIn('## 审阅状态与源码统计', navigation)
        self.assertNotIn('## 功能点清单', navigation)
        self.assertEqual(text.count('## 功能点清单'), 1)
        self.assertTrue(any('behavior' in error for error in completeness.section_errors(text)))
        self.assertTrue(all(c['status'] == 'unknown' for c in claims if c['id'] != 'run-success'))
        self.assertEqual(generated['review']['scope'], 'sampled')

    def test_delivery_option_cannot_change_scope_during_draft_generation(self):
        helper = self.helper()
        case, reports, mapping, ledger, preview, captured = helper.preview()
        before = helper.final_bytes(case, mapping)
        result = helper.cli(case, 'draft.py', '--delivery', 'complete')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('only to --promote', result.stderr)
        self.assertEqual(helper.final_bytes(case, mapping), before)


if __name__ == '__main__': unittest.main()
