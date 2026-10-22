"""Restrict generated guides and preview artifacts to the documented write scope."""
from pathlib import Path
import re
import unicodedata


def contained_path(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError(f'expected normalized project-relative path: {relative}')
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or p.as_posix() != relative or relative == '.':
        raise ValueError(f'expected normalized project-relative path: {relative}')
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'symlink guide/artifact target not allowed: {relative}')
    if not current.resolve().is_relative_to(root):
        raise ValueError(f'target outside project: {relative}')
    if current.exists() and (not current.is_file() or current.stat().st_nlink > 1):
        raise ValueError(f'target must be an unshared regular file: {relative}')
    return current


def guide_path(root, document):
    target = contained_path(root, document)
    if not re.fullmatch(r'(?i)AGENTS(?:\.[^. /][^/]*)?\.md', target.name):
        raise ValueError(f'guide target must be AGENTS.md or AGENTS.<module>.md: {document}')
    parts = Path(document).parts[:-1]
    if any(part.startswith('.') for part in parts) and not (
        len(parts) >= 2 and parts[:2] == ('.agentsmd', 'drafts') and not any(p.startswith('.') for p in parts[2:])):
        raise ValueError(f'guide target in protected/analysis directory: {document}')
    if any(part in {'node_modules', 'vendor', 'dist', 'build', 'target'} for part in parts):
        raise ValueError(f'guide target in dependency/build directory: {document}')
    return target


def validate_targets(root, documents):
    paths, seen = [], set()
    for document in documents:
        path = guide_path(root, document)
        key = unicodedata.normalize('NFC', str(path)).casefold()
        if key in seen:
            raise ValueError(f'duplicate guide target (including case/path aliases): {document}')
        seen.add(key)
        paths.append(path)
    return paths


def preview_directory(root, directory):
    p = Path(directory)
    if p.parts[:2] != ('.agentsmd', 'drafts') or '..' in p.parts:
        raise ValueError('preview output must be .agentsmd/drafts or a subdirectory')
    # Validate components without requiring the directory itself to be a file.
    contained_path(root, (p / 'modules.draft.json').as_posix())
    return Path(root).resolve() / p
