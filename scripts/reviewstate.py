"""Freeze explicit reading records against whole owned-source contents."""
import copy
from datetime import datetime, timezone
from pathlib import Path
from artifacts import file_map, safe_path
from scanpolicy import digest
from schemas import mapping_errors, require


def freshness(root, module, files):
    review = module.get('review', {})
    recorded = review.get('source_hashes', {})
    owned = set(module.get('files', []))
    errors = []
    if not owned: return errors
    if set(recorded) != owned:
        errors.append(f"{module['id']}: review snapshot missing or ownership changed; reread and record-review")
    for path in sorted(owned & set(recorded)):
        try:
            actual = digest(safe_path(root, path).read_bytes())
            if recorded[path] != actual:
                errors.append(f"{module['id']}: review stale source: {path}; reread and record-review")
            indexed = files.get(path, {}).get('content_hash')
            if indexed and indexed != actual:
                errors.append(f"{module['id']}: review source index stale: {path}; reanalyze")
        except (OSError, ValueError) as exc:
            errors.append(f"{module['id']}: review source unavailable: {path}: {exc}")
    return errors


def record(root, mapping, reports, module_ids):
    require(mapping_errors(mapping))
    root = Path(root).resolve()
    if Path(mapping['root']).resolve() != root:
        raise ValueError('module map root mismatch')
    if mapping.get('scope') != reports[0].get('scope'):
        raise ValueError('module map scope stale; reconcile ownership first')
    output = copy.deepcopy(mapping)
    by_id = {m['id']: m for m in output['modules']}
    if len(by_id) != len(output['modules']): raise ValueError('duplicate module id')
    files = file_map(reports)
    for mid in module_ids:
        if mid not in by_id: raise ValueError(f'unknown module to record: {mid}')
        module = by_id[mid]
        review = module.get('review', {})
        if review.get('scope') not in ('sampled', 'complete'):
            raise ValueError(f'{mid}: declare sampled/complete reading scope before record-review')
        dispositions = review.get('reviewed_files', []) + [entry['path'] for entry in review.get('exclusions', [])]
        if not set(dispositions) <= set(module['files']):
            raise ValueError(f'{mid}: review disposition refers to unowned source')
        hashes = {}
        for path in module['files']:
            if path not in files: raise ValueError(f'{mid}: source absent from reports: {path}')
            actual = digest(safe_path(root, path).read_bytes())
            if files[path].get('content_hash') and actual != files[path]['content_hash']:
                raise ValueError(f'{mid}: source index stale: {path}; reanalyze before recording')
            hashes[path] = actual
        review['source_hashes'] = hashes
        review['recorded_at'] = datetime.now(timezone.utc).isoformat()
        module['review'] = review
    # No scope, capability, evidence status or test outcome is promoted here.
    return output
