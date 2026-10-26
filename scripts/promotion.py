"""Publish preview text and evidence together, without refreshing source proofs."""
import copy
from pathlib import Path
from artifacts import file_map, safe_path, report_items
from citations import validate_citations
from draft import relocate_links
from guidepaths import contained_path, preview_directory, validate_targets
from guidewrites import apply, check_pending, json_bytes
import evidence
import modules
from scanpolicy import current_selection, digest
from schemas import mapping_errors, ledger_errors, reproducible_scope_errors, require


def promote(root, mapping, ledger, reports, inputs=None, after_replace=None, delivery='complete'):
    root = Path(root).resolve()
    require(mapping_errors(mapping) + ledger_errors(ledger))
    if delivery not in ('complete', 'sampled'):
        raise ValueError('promotion delivery must be complete or explicitly sampled')
    if mapping.get('delivery', {}).get('mode') != delivery:
        raise ValueError(f'promotion requires delivery.mode={delivery}; finish module reading and evidence for complete delivery; '
                         'use --delivery sampled only for an explicitly requested partial delivery')
    check_pending(root)
    if Path(mapping['root']).resolve() != root: raise ValueError('module map root mismatch')
    if mapping.get('delivery', {}).get('scope_confirmed') is not True:
        raise ValueError('confirm module boundaries before promoting guides')
    if 'preview' not in mapping or any('final_document' not in m for m in mapping['modules']):
        raise ValueError('preview lacks final target/baseline metadata; regenerate from the reviewed final module map')
    directory = preview_directory(root, mapping['preview']['directory'])
    for module in mapping['modules']:
        if not safe_path(root, module['document']).is_relative_to(directory):
            raise ValueError(f'{module["id"]}: source document is outside declared preview directory')
        if Path(module['final_document']).parts[0] == '.agentsmd':
            raise ValueError('final guide target must be outside the analysis store')
    validate_targets(root, [m['document'] for m in mapping['modules']])
    validate_targets(root, [m['final_document'] for m in mapping['modules']])
    source_bytes = {m['document']: safe_path(root, m['document']).read_bytes() for m in mapping['modules']}
    if not reports or current_selection(root, reports[0].get('scope'))['scope'] != reports[0].get('scope'):
        raise ValueError('analysis selection is stale; reanalyze and reconcile the preview before promotion')
    for path, file in file_map(reports).items():
        if file.get('content_hash') and digest(safe_path(root, path).read_bytes()) != file['content_hash']:
            raise ValueError(f'{path}: source index stale; reanalyze before promotion')
    # Check captured prose/citations/live source before changing any link or hash.
    complete = delivery == 'complete'
    errors, _ = modules.check(root, mapping, reports, require_reviewed=complete)
    errs, _ = evidence.check(root, ledger, mapping, reports, require_modules=complete)
    errors.extend(errs)
    if complete:
        for i, report in enumerate(reports):
            errors.extend(reproducible_scope_errors(report.get('scope'), f'reports[{i}].scope'))
    require(errors)
    from verify import validate
    named_reports = report_items(root, reports)
    root_document = next(m['document'] for m in mapping['modules'] if m['id'] == mapping['root_module'])
    errs, _, _ = validate(root, named_reports, list(source_bytes), root_document,
                          {path: raw.decode('utf-8') for path, raw in source_bytes.items()})
    errors.extend(errs)
    require(errors)
    source_to_final = {m['document']: m['final_document'] for m in mapping['modules']}
    output, captured = copy.deepcopy(mapping), copy.deepcopy(ledger)
    baseline = {m['final_document']: m['final_document_hash'] for m in mapping['modules']}
    baseline.update({'.agentsmd/' + name: hashed for name, hashed in mapping['preview']['artifact_hashes'].items()
                     if name in ('modules.json', 'evidence.json')})
    dependencies = dict(inputs or {})
    writes, parsed = {}, {}
    for module in output['modules']:
        source, final = module['document'], module['final_document']
        source_path = safe_path(root, source)
        original = source_bytes[source]
        if source_path.read_bytes() != original: raise ValueError(f'preview input changed: {source}; rerun promotion')
        dependencies[source_path] = original
        text = relocate_links(root, original.decode('utf-8'), source, final, source_to_final)
        writes[final] = text.encode('utf-8')
        parsed[final] = evidence.parse_blocks(text, final)
        module['document'] = final
        del module['final_document'], module['final_document_hash']
    del output['preview']
    for claim in captured['claims']:
        final = source_to_final[claim['document']]
        statement = parsed[final][claim['id']]
        require(validate_citations(root, final, statement, claim['evidence'], writes[final].decode('utf-8')))
        claim['document'], claim['statement'], claim['statement_hash'] = final, statement, digest(statement)
        # Source/snippet/symbol hashes, claim status and review/test records stay intact.
    # Verify the relocated documents before writing. Links to planned guides are
    # checked against this batch, so new final targets need not exist yet.
    final_texts = {path: raw.decode('utf-8') for path, raw in writes.items()}
    final_root = source_to_final[root_document]
    errors, _, _ = validate(root, named_reports, list(final_texts), final_root, final_texts)
    require(errors)
    writes['.agentsmd/modules.json'] = json_bytes(output)
    writes['.agentsmd/evidence.json'] = json_bytes(captured)
    originals = {}
    for relative, desired in writes.items():
        path = contained_path(root, relative)
        actual = path.read_bytes() if path.exists() else None
        if (digest(actual) if actual is not None else None) != baseline[relative] and actual != desired:
            raise ValueError(f'promotion target changed since preview: {relative}; reconcile changes before regenerating the baseline')
        originals[relative] = actual
    apply(root, writes, originals, after_replace=after_replace, dependencies=dependencies)
    return output, captured
