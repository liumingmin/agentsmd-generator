"""Shared report loading and project-contained artifact paths."""
import json
from pathlib import Path


def read_json(path):
    from snapshots import read_report
    return read_report(path)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def safe_path(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f'expected project-relative path: {relative}')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'path outside project: {relative}')
    return path


def load_reports(root, paths=None, pinned=None):
    root = Path(root).resolve()
    import snapshots
    pinned = dict(pinned or {})
    if not paths:
        directory = root / '.agentsmd'
        key = str(directory.resolve())
        if key not in pinned:
            pinned[key] = snapshots.manifest(directory)
        paths = snapshots.paths(directory, pinned=pinned[key])
    else:
        paths = list(paths)
    # Pin each manifest once even if publication happens between individual reads.
    reports = []
    for path in paths:
        path = Path(path)
        key = str(path.parent.resolve())
        if key not in pinned:
            pinned[key] = snapshots.manifest(path.parent)
        report = snapshots.read_report(path, pinned[key])
        from schemas import report_errors, require
        require(report_errors(report, str(path)))
        reports.append(report)
    if any(value is None and snapshots.manifest(directory) is not None for directory, value in pinned.items()):
        raise ValueError('analysis generation appeared while reading legacy reports; retry')
    if not reports:
        raise ValueError('no analysis reports found')
    for report in reports:
        if Path(report['root']).resolve() != root:
            raise ValueError('report root does not match project root')
    generations = {r.get('snapshot_id') for r in reports}
    if len(generations) > 1:
        raise ValueError('reports belong to different analysis generations')
    scopes = [r.get('scope', {}).get('selection_hash') for r in reports]
    if len(set(scopes)) > 1:
        raise ValueError('reports use different selection scopes; rerun analyze.py')
    return reports


def file_map(reports):
    files = {}
    for report in reports:
        for file in report.get('files') or []:
            before = files.get(file['path'], {})
            if before.get('content_hash') and file.get('content_hash') and before['content_hash'] != file['content_hash']:
                raise ValueError(f"inconsistent snapshots: {file['path']}")
            files[file['path']] = {**before, **file}
    return files
