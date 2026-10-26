"""Keep declared delivery, document structure and preview evidence consistent."""
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import artifacts
import completeness
import draft
import evidence
import promotion
import test_publication


class DeliveryConsistency(unittest.TestCase):
    def helper(self):
        helper = test_publication.Publication()
        self.addCleanup(helper.doCleanups)
        return helper

    def test_default_verify_enforces_complete_map_and_rejects_weaker_mode_flags(self):
        helper = self.helper()
        case, reports, mapping, ledger, preview, captured = helper.preview()
        preview['modules'][1]['review']['scope'] = 'sampled'
        doc = case.root / preview['modules'][1]['document']
        doc.write_text(doc.read_text() + '\n抽样阅读，未完成模块。\n')
        path = case.root / '.agentsmd/drafts/modules.draft.json'
        artifacts.write_json(path, preview)
        for flags in ([], ['--delivery', 'draft'], ['--delivery', 'sampled']):
            result = helper.cli(case, 'verify.py', '--modules', str(path), *flags, '--json')
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('review scope is sampled', result.stdout)
        # Intermediate maps may still receive an explicitly partial structural check.
        preview['delivery']['mode'] = 'draft'
        artifacts.write_json(path, preview)
        result = helper.cli(case, 'verify.py', '--modules', str(path), '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = helper.cli(case, 'verify.py', '--modules', str(path), '--delivery', 'sampled', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertIn('delivery mode mismatch', result.stdout)

    def test_complete_promotion_rejects_dead_links_range_errors_and_wrong_stats_before_writes(self):
        helper = self.helper()
        case, reports, mapping, ledger, preview, captured = helper.preview()
        doc = case.root / preview['modules'][0]['document']; original = doc.read_text()
        before = helper.final_bytes(case, mapping)
        probes = [('[example](missing-example.json)', '死链'),
                  ('[source](../../src/service.py#L999)', '超出源码行数'),
                  ('<!-- agentsmd:stats {"source":".agentsmd/index.json","file_count":999} -->', '文档值')]
        for text, message in probes:
            with self.subTest(text=text):
                doc.write_text(original + '\n' + text + '\n')
                with self.assertRaisesRegex(ValueError, message):
                    promotion.promote(case.root, preview, captured, reports)
                self.assertEqual(helper.final_bytes(case, mapping), before)
        doc.write_text(original)
        promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(helper.cli(case, 'verify.py', '--delivery', 'complete', '--json').returncode, 0)

    def test_relocated_new_guide_targets_are_validated_against_the_publication_batch(self):
        helper = self.helper(); case = helper.case()
        reports, mapping, module, ledger = case.fixture(); case.complete(mapping, module)
        destination = 'docs/new/AGENTS.service.md'
        old = module['document']
        authored = (case.root / old).read_text()
        module['document'] = destination
        ledger['claims'][0]['document'] = destination
        helper.save(case, reports, mapping, ledger)
        preview, claims = draft.generate(case.root, mapping, reports, ledger)
        child = next(m for m in preview['modules'] if m['id'] == module['id'])
        parent = next(m['document'] for m in preview['modules'] if m['id'] == preview['root_module'])
        # Write reviewed text into the preview while its final target is absent.
        (case.root / child['document']).write_text(draft.relocate_links(case.root, authored, old,
            child['document'], {'AGENTS.md': parent}))
        captured = evidence.capture(case.root, {'claims': claims}, reports)
        final = case.root / destination
        self.assertFalse(final.exists())
        self.assertIsNone(child['final_document_hash'])
        promotion.promote(case.root, preview, captured, reports)
        self.assertTrue(final.exists())
        self.assertEqual(helper.cli(case, 'verify.py', '--delivery', 'complete', '--json').returncode, 0)

    def test_custom_report_paths_remain_available_to_publication_stats_validation(self):
        helper = self.helper()
        case, reports, mapping, ledger, preview, captured = helper.preview()
        paths = [case.root / '.agentsmd/custom-context.json', case.root / '.agentsmd/custom-index.json']
        for path, report in zip(paths, reports): artifacts.write_json(path, report)
        loaded = artifacts.load_reports(case.root, paths)
        doc = case.root / preview['modules'][0]['document']
        count = loaded[1]['stats']['file_count']
        doc.write_text(doc.read_text() + f'\n<!-- agentsmd:stats {{"source":".agentsmd/custom-index.json","file_count":{count}}} -->\n')
        promotion.promote(case.root, preview, captured, loaded)
        self.assertNotIn('source', artifacts.read_json(paths[1]))

    def test_feature_table_from_template_is_authored_content_and_navigation_is_not(self):
        helper = self.helper()
        case, reports, mapping, ledger, preview, captured = helper.preview()
        module = next(m for m in preview['modules'] if m['files'])
        doc = case.root / module['document']
        table = ('## 功能点清单\n\n| 功能 | 输入/触发 | 核心行为与输出 | 关键实现 |\n'
                 '| --- | --- | --- | --- |\n'
                 '| 计算 | 调用 run(value) | 检查参数后返回计算值，负数触发异常。 | [源码](../../src/service.py#L1-L3) |\n\n')
        doc.write_text(re.sub(r'## 主要功能\n.*?(?=## 依赖与协作)', lambda _: table, doc.read_text(), flags=re.S))
        self.assertEqual(completeness.section_errors(doc.read_text()), [])
        promotion.promote(case.root, preview, captured, reports)
        self.assertEqual(helper.cli(case, 'verify.py', '--delivery', 'complete', '--strict', '--json').returncode, 0)
        for body in ('| 功能 | 行为 |\n| --- | --- |\n',
                     '| 功能 | 行为 |\n| --- | --- |\n| [run](service.py) | [source](service.py) |\n',
                     '| 类别 | 文件 | 符号 |\n| --- | --- | --- |\n| production | 1 | 3 |\n',
                     '| 功能 | 行为 |\n| --- | --- |\n| TODO | 待补充 |\n'):
            self.assertTrue(any('behavior' in e for e in completeness.section_errors('## 功能点清单\n' + body)))

    def test_custom_preview_map_selects_its_own_ledger_for_capture_verify_and_promotion(self):
        helper = self.helper()
        case, reports, mapping, ledger, old, old_captured = helper.preview()
        preview, claims = draft.generate(case.root, mapping, reports, ledger, new_preview=True,
                                         out_dir='.agentsmd/drafts/second')
        captured = evidence.capture(case.root, {'claims': claims}, reports)
        ep = case.root / '.agentsmd/drafts/second/evidence.draft.json'
        mp = case.root / '.agentsmd/drafts/second/modules.draft.json'
        artifacts.write_json(ep, captured)
        final_ledger = (case.root / '.agentsmd/evidence.json').read_bytes()
        original_session = (case.root / '.agentsmd/drafts/evidence.draft.json').read_bytes()
        result = helper.cli(case, 'evidence.py', 'capture', '--map', str(mp))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((case.root / '.agentsmd/evidence.json').read_bytes(), final_ledger)
        self.assertEqual((case.root / '.agentsmd/drafts/evidence.draft.json').read_bytes(), original_session)
        result = helper.cli(case, 'verify.py', '--modules', str(mp), '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = helper.cli(case, 'verify.py', '--modules', str(mp), '--evidence', str(case.root / '.agentsmd/drafts/evidence.draft.json'), '--json')
        self.assertEqual(result.returncode, 1)
        before = helper.final_bytes(case, mapping)
        result = helper.cli(case, 'draft.py', '--promote', '--map', str(mp), '--ledger', str(case.root / '.agentsmd/drafts/evidence.draft.json'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(helper.final_bytes(case, mapping), before)
        result = helper.cli(case, 'draft.py', '--promote', '--map', str(mp))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(helper.cli(case, 'verify.py', '--json').returncode, 0)


if __name__ == '__main__': unittest.main()
