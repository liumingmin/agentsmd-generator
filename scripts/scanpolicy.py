#!/usr/bin/env python3
"""Shared file selection, source classification, and stable symbol metadata.

The preferred pipeline creates one selection.json and passes it to every indexer.
Git selects tracked + unignored untracked files; hard exclusions still apply.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

DEFAULT_EXCLUDES = ['.git', '.agentsmd', '.workbuddy', 'node_modules', 'vendor', 'dist',
                    'build', 'target', '__pycache__', '.venv', 'venv', 'coverage', '.DS_Store']
HIDDEN_DIRS = {'.github', '.circleci'}
SOURCE_EXTS = {'.go', '.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.mts', '.cts',
               '.java', '.kt', '.rs', '.c', '.h', '.cc', '.cpp', '.hpp', '.sql', '.proto',
               '.sh', '.bash', '.rb', '.php', '.cs', '.ps1'}


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf-8')).hexdigest()


def is_test(path):
    p = Path(path)
    return (any(part.lower() in {'test', 'tests', '__tests__', 'testdata'} for part in p.parts[:-1])
            or bool(re.search(r'(^test_|_test\.|\.test\.|\.spec\.|Tests?\.(java|kt)$)', p.name)))


def classify(path, text='', generated=None):
    test = is_test(path)
    if generated is None:
        generated = bool(re.search(r'(?im)^.{0,10}(?:Code generated\b[^\n]*DO NOT EDIT|@generated\b)', text[:8192]))
    return {'is_test': test, 'generated': bool(generated),
            'category': 'test' if test else ('generated' if generated else 'production')}


def counts(files):
    result = {category: {'file_count': 0, 'symbol_count': 0} for category in ('production', 'test', 'generated')}
    for file in files:
        category = file.get('category') or classify(file['path'], generated=file.get('generated', False))['category']
        result[category]['file_count'] += 1
        result[category]['symbol_count'] += len(file.get('symbols') or [])
    return result


def finalize_symbols(symbols):
    groups = {}
    for symbol in symbols:
        groups.setdefault((symbol['kind'], symbol['name']), []).append(symbol)
    for (kind, name), members in groups.items():
        signatures = {}
        for symbol in members:
            fingerprint = symbol.get('signature_hash') or ''
            signatures[fingerprint] = signatures.get(fingerprint, 0) + 1
        used = {}
        for symbol in members:
            base = f'{kind}:{name}'
            signature = symbol.get('signature_hash')
            content = symbol.get('content_hash')
            if len(members) == 1:
                identity, quality = base + '#0', 'unique-name'
            elif signature and signatures[signature] == 1:
                identity, quality = base + '@' + signature[:16], 'signature'
            elif content:
                identity, quality = base + '@' + content[:16], 'content'
            else:
                identity, quality = base, 'declaration-order'
            ordinal = used.get(identity, 0)
            used[identity] = ordinal + 1
            symbol['id'] = identity + (f'#{ordinal}' if quality == 'declaration-order' or ordinal else '')
            symbol['identity_quality'] = quality if not ordinal else 'ambiguous-identical'
    return symbols


def matches(path, pattern):
    pattern = pattern.replace('\\', '/').strip('/')
    if not pattern:
        return False
    if '/' not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in path.split('/'))
    return fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern + '/')


def policy_options(root, excludes=(), respect_gitignore=None):
    config = Path(root) / '.agentsmd/config.json'
    data = json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
    if not isinstance(data, dict):
        raise ValueError('config must be an object')
    hidden = data.get('include_hidden', [])
    if not isinstance(hidden, list) or any(not isinstance(item, str) for item in hidden):
        raise ValueError('config.include_hidden must be an array of directory names')
    if not isinstance(data.get('respect_gitignore', True), bool):
        raise ValueError('config.respect_gitignore must be boolean')
    patterns = data.get('exclude', [])
    if not isinstance(patterns, list) or any(not isinstance(item, str) for item in patterns):
        raise ValueError('config.exclude must be an array of path patterns')
    return {'exclude': sorted(set(DEFAULT_EXCLUDES + patterns + list(excludes))),
            'respect_gitignore': data.get('respect_gitignore', True) if respect_gitignore is None else respect_gitignore,
            'include_hidden': sorted(set(HIDDEN_DIRS) | set(data.get('include_hidden', [])))}


def select_files(root, excludes=(), respect_gitignore=None):
    root = Path(root).resolve()
    excludes = list(excludes)
    if not root.is_dir():
        raise ValueError(f'Not a directory: {root}')
    policy = policy_options(root, excludes, respect_gitignore)
    diagnostics, selected = [], []
    git_files = None
    if policy['respect_gitignore']:
        try:
            run = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                                  '--exclude-standard', '-z', '--', '.'], capture_output=True, check=True)
            git_files = {os.fsdecode(path).replace('\\', '/') for path in run.stdout.split(b'\0') if path}
        except (OSError, subprocess.CalledProcessError):
            pass  # Non-Git snapshots use the same explicit exclusions.
    def rejected(path):
        return any(matches(path, pattern) for pattern in policy['exclude'])
    def walk_error(exc):
        diagnostics.append({'path': str(exc.filename), 'reason': str(exc)})
    for current, dirs, files in os.walk(root, onerror=walk_error):
        rel_dir = Path(current).relative_to(root)
        for name in dirs:
            if (Path(current) / name).is_symlink() and not rejected((rel_dir / name).as_posix()):
                diagnostics.append({'path': (rel_dir / name).as_posix(), 'reason': 'symlink directory not followed'})
        dirs[:] = sorted(name for name in dirs if not (Path(current) / name).is_symlink() and not rejected((rel_dir / name).as_posix())
                         and (not name.startswith('.') or name in policy['include_hidden']))
        for name in sorted(files):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if rejected(rel) or re.fullmatch(r'(?i)agents(?:\..+)?\.md', name):
                continue
            if git_files is not None and rel not in git_files:
                continue
            if path.is_symlink():
                diagnostics.append({'path': rel, 'reason': 'symlink not followed'})
                continue
            if path.is_file():
                selected.append(rel)
    selected.sort()
    scope = {'policy': policy, 'options': {'exclude': sorted(set(excludes)), 'respect_gitignore': respect_gitignore},
             'gitignore_applied': git_files is not None,
             'selection_hash': digest('\n'.join(selected))}
    return {'schema_version': 1, 'root': str(root), 'files': selected, 'scope': scope, 'diagnostics': diagnostics}


def current_selection(root, scope):
    """Reapply explicit CLI options to today's config, never replay merged policy."""
    options = scope.get('options') if isinstance(scope, dict) else None
    if not isinstance(options, dict) or 'exclude' not in options or 'respect_gitignore' not in options:
        raise ValueError('legacy scan scope lacks option provenance; rerun analyze.py to rebuild the selection')
    excludes, respect = options['exclude'], options['respect_gitignore']
    if not isinstance(excludes, list) or any(not isinstance(p, str) for p in excludes) or (respect is not None and type(respect) is not bool):
        raise ValueError('invalid scan option provenance; rerun analyze.py')
    return select_files(root, excludes, respect)


def load_selection(root, selection=None, excludes=(), respect_gitignore=None):
    root = Path(root).resolve()
    if selection is None:
        return select_files(root, excludes, respect_gitignore)
    from artifacts import read_json
    from schemas import require, selection_errors
    data = read_json(selection) if not isinstance(selection, dict) else selection
    require(selection_errors(data))
    if Path(data['root']).resolve() != root:
        raise ValueError('selection root does not match project root')
    if len(set(data['files'])) != len(data['files']):
        raise ValueError('duplicate selection paths')
    for rel in data['files']:
        if Path(rel).as_posix() != rel or Path(rel).is_absolute() or '..' in Path(rel).parts or (root / rel).is_symlink() or not (root / rel).resolve().is_relative_to(root):
            raise ValueError(f'unsafe selection path: {rel}')
    if data['scope']['selection_hash'] != digest('\n'.join(sorted(data['files']))):
        raise ValueError('selection hash does not match file list')
    return data


def add_arguments(parser):
    parser.add_argument('--selection', help='shared selection.json; root must match')
    parser.add_argument('--exclude', action='append', default=[], help='additional path pattern; repeatable')
    parser.add_argument('--no-gitignore', action='store_true', help='include Git-ignored files except explicit exclusions')


def from_args(args):
    return load_selection(args.root, args.selection, args.exclude, False if args.no_gitignore else None)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    add_arguments(parser)
    args = parser.parse_args()
    print(json.dumps(from_args(args), ensure_ascii=False, indent=2))
