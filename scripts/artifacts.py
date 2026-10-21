"""Shared report loading and project-contained artifact paths."""
import json
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


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


def load_reports(root, paths=None):
    root = Path(root).resolve()
    paths = paths or [root / '.agentsmd' / name for name in ('context.json', 'ast.json', 'index.json')
                      if (root / '.agentsmd' / name).is_file()]
    reports = [read_json(path) for path in paths]
    if not reports:
        raise ValueError('no analysis reports found')
    for report in reports:
        if Path(report['root']).resolve() != root:
            raise ValueError('report root does not match project root')
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
