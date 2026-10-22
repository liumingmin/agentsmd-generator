"""Resume one persisted preview session without rebasing it onto edited targets."""
import json
from pathlib import Path
from guidepaths import contained_path, preview_directory
from schemas import mapping_errors, ledger_errors, require


def layout(mapping):
    return {'root': mapping['root'], 'root_module': mapping['root_module'], 'scope': mapping.get('scope'),
            'modules': sorted(((m['id'], m.get('parent'), tuple(sorted(m.get('children', []))),
                               tuple(sorted(m['files'])), m.get('final_document', m['document']))
                              for m in mapping['modules']), key=lambda item: item[0])}


def prepare(root, mapping, ledger, out_dir, new_preview=False):
    directory = preview_directory(root, out_dir)
    if directory.exists() and not directory.is_dir(): raise ValueError('preview output must be a directory')
    occupied = directory.exists() and any(p.is_file() or p.is_symlink() for p in directory.rglob('*'))
    paths = [contained_path(root, (directory / name).relative_to(root).as_posix())
             for name in ('modules.draft.json', 'evidence.draft.json')]
    if new_preview:
        if 'preview' in mapping: raise ValueError('--new-preview requires a final module map')
        if occupied:
            raise ValueError('new preview requires an empty --out-dir; existing sessions are never reset')
        return mapping, ledger, {}
    if not paths[0].exists():
        if 'preview' in mapping or occupied:
            raise ValueError('preview session metadata missing; preserve existing files and use a new empty --out-dir')
        return mapping, ledger, {}
    if not paths[1].exists(): raise ValueError('preview evidence metadata missing; recover or reconcile the existing session')
    dependencies = {p: p.read_bytes() for p in paths}
    saved, captured = [json.loads(dependencies[p]) for p in paths]
    require(mapping_errors(saved) + ledger_errors(captured, draft=True))
    if 'preview' not in saved or saved['preview']['directory'] != out_dir:
        raise ValueError('legacy or mismatched preview session; preserve it and use a new empty --out-dir')
    if layout(mapping) != layout(saved):
        raise ValueError('preview layout/scope changed; reconcile the preview map or create a new session in an empty --out-dir')
    if 'preview' in mapping:
        baseline = lambda value: (value['preview'], {m['id']: (m.get('final_document'), m.get('final_document_hash')) for m in value['modules']})
        if baseline(mapping) != baseline(saved):
            raise ValueError('preview baseline changed; existing session baselines must be preserved')
        return mapping, ledger if ledger is not None else captured, dependencies
    # The ordinary CLI reads the final map, but a continuing preview owns its
    # authored reading/capability/test records and captured evidence.
    return saved, captured, dependencies
