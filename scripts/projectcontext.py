#!/usr/bin/env python3
"""Inventory project assets and extract traceable manifest facts (standard library).

Every entry is a reading hint, not a conclusion about business behavior. File
hashes allow diff.py to detect changes in config, CI, docs and manifests too.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

SKIP_DIRS = {'.git', '.agentsmd', '.workbuddy', 'node_modules', 'vendor', 'dist',
             'build', 'target', '__pycache__', '.venv', 'venv', 'coverage'}
SOURCE_EXTS = {'.go', '.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.java',
               '.kt', '.rs', '.c', '.h', '.cc', '.cpp', '.hpp', '.sql', '.proto',
               '.sh', '.bash', '.rb', '.php', '.cs'}
MANIFESTS = {'go.mod', 'go.work', 'package.json', 'pnpm-workspace.yaml',
             'requirements.txt', 'pyproject.toml', 'Pipfile', 'Cargo.toml',
             'pom.xml', 'build.gradle', 'build.gradle.kts', 'Gemfile', 'composer.json'}
LOCKS = {'go.sum', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'uv.lock',
         'poetry.lock', 'Cargo.lock', 'Pipfile.lock', 'Gemfile.lock', 'composer.lock'}
BUILD = {'Makefile', 'GNUmakefile', 'justfile', 'Taskfile.yml', 'CMakeLists.txt'}
ENTRY_NAMES = {'main.go', 'main.py', 'main.rs', 'manage.py', '__main__.py',
               'index.ts', 'index.js', 'main.ts', 'main.js', 'app.py', 'server.ts',
               'server.js', 'server.py', 'wsgi.py', 'asgi.py', 'Program.cs'}


def is_test(path):
    p = Path(path)
    return (any(part.lower() in {'test', 'tests', '__tests__'} for part in p.parts[:-1])
            or bool(re.search(r'(^test_|_test\.|\.test\.|\.spec\.|Tests?\.(java|kt)$)', p.name)))


def categories(path):
    p = Path(path)
    name, ext = p.name, p.suffix.lower()
    tags = []
    if ext in SOURCE_EXTS:
        tags.append('source')
    if is_test(path):
        tags.append('test')
    if name in MANIFESTS or re.fullmatch(r'requirements[\w.-]*\.txt', name):
        tags.append('manifest')
    if name in LOCKS:
        tags.append('lockfile')
    if ext in {'.md', '.rst', '.adoc'}:
        tags.append('doc')
    if name in ENTRY_NAMES:
        tags.append('entrypoint_candidate')
    if name in BUILD or name.startswith('Dockerfile') or name.startswith(('compose.', 'docker-compose.')):
        tags.append('build_deploy')
    if (path.startswith(('.github/workflows/', '.circleci/'))
            or name in {'.gitlab-ci.yml', 'Jenkinsfile', '.travis.yml', 'azure-pipelines.yml'}):
        tags.append('ci')
    if ext in {'.yaml', '.yml', '.toml', '.ini', '.conf', '.properties', '.json'} or name.startswith('.env'):
        tags.append('config')
    if any(part.lower() in {'migrations', 'migration', 'schema', 'schemas', 'seeds'} for part in p.parts[:-1]):
        tags.append('migration_schema')
    return tags or ['other']


def manifest_facts(path, text):
    """Only extract syntax we can parse; retain precise source locations."""
    facts = []
    lines = text.splitlines()
    if Path(path).name == 'package.json':
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('package.json must be an object')
        for key in ('name', 'version', 'packageManager', 'engines', 'workspaces', 'bin', 'main', 'exports'):
            if key in data:
                facts.append({'kind': key, 'value': data[key], 'json_pointer': '/' + key})
        for section in ('scripts', 'dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
            values = data.get(section, {})
            if isinstance(values, dict):
                for name, value in sorted(values.items()):
                    escaped = name.replace('~', '~0').replace('/', '~1')
                    facts.append({'kind': section, 'name': name, 'value': value,
                                  'json_pointer': '/' + section + '/' + escaped})
    elif Path(path).name == 'go.mod':
        block = ''
        for n, line in enumerate(lines, 1):
            code = line.split('//', 1)[0].strip()
            if code == ')':
                block = ''
                continue
            match = re.fullmatch(r'(require|replace|exclude|use)\s*\(', code)
            if match:
                block = match[1]
                continue
            match = re.match(r'(module|go|toolchain|require|replace|exclude)\s+(.+)', code)
            if match:
                facts.append({'kind': match[1], 'value': match[2], 'line': n})
            elif code and block:
                facts.append({'kind': block, 'value': code, 'line': n})
    elif Path(path).name in BUILD:
        for n, line in enumerate(lines, 1):
            match = re.match(r'^([\w.-]+(?: +[\w.-]+)*):(?!=)', line)
            if match:
                facts.append({'kind': 'target_candidate', 'value': match[1], 'line': n})
    return facts


def scan(root, max_bytes=2_000_000):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Not a directory: {root}')
    files, diagnostics, grouped, dirs = [], [], {}, {}
    def walk_error(exc):
        diagnostics.append({'path': str(exc.filename), 'reason': str(exc)})

    for current, names, filenames in os.walk(root, onerror=walk_error):
        names[:] = sorted(n for n in names if n not in SKIP_DIRS
                          and (not n.startswith('.') or n in {'.github', '.circleci'}))
        for name in sorted(filenames):
            path = Path(current) / name
            rel = path.relative_to(root).as_posix()
            if re.fullmatch(r'(?i)agents(?:\..+)?\.md', name):
                continue  # Generated guides must not invalidate their own snapshot.
            if path.is_symlink():
                diagnostics.append({'path': rel, 'reason': 'symlink not followed'})
                continue
            tags = categories(rel)
            record = {'path': rel, 'categories': tags}
            try:
                size = path.stat().st_size
                record['size_bytes'] = size
                if size > max_bytes:
                    diagnostics.append({'path': rel, 'reason': 'size limit; content not inspected'})
                else:
                    raw = path.read_bytes()
                    record['content_hash'] = hashlib.sha256(raw).hexdigest()
                    text = raw.decode('utf-8-sig') if tags != ['other'] else ''
                    record['line_end'] = len(text.splitlines())
                    if tags == ['other']:
                        del record['line_end']
                    if '\x00' in text:
                        raise ValueError('binary content in a text asset; not inspected')
                    if 'manifest' in tags or 'build_deploy' in tags:
                        record['facts'] = manifest_facts(rel, text)
                    if 'source' in tags and re.search(r'(?m)^\s*(?:func main\(|if __name__\s*==)', text):
                        if 'entrypoint_candidate' not in tags:
                            tags.append('entrypoint_candidate')
                    # Include names only: actual credentials and environment values stay out of the report.
                    if name.startswith('.env'):
                        record['environment_keys'] = [
                            {'name': m.group(1), 'line': n}
                            for n, line in enumerate(text.splitlines(), 1)
                            if (m := re.match(r'\s*(?:export\s+)?([A-Za-z_]\w*)\s*=', line))]
            except (OSError, UnicodeError, ValueError) as exc:
                diagnostics.append({'path': rel, 'reason': str(exc)})
            files.append(record)
            for tag in tags:
                grouped.setdefault(tag, []).append(rel)
            directory = rel.split('/')[0] if '/' in rel else '.'
            summary = dirs.setdefault(directory, {'files': 0, 'source_files': 0, 'test_files': 0})
            summary['files'] += 1
            summary['source_files'] += int('source' in tags)
            summary['test_files'] += int('test' in tags)
    files.sort(key=lambda f: f['path'])
    return {'schema_version': 2, 'root': str(root), 'files': files,
            'categories': {k: sorted(v) for k, v in sorted(grouped.items())},
            'directories': dict(sorted(dirs.items())),
            'stats': {'file_count': len(files), **{k + '_count': len(v) for k, v in sorted(grouped.items())}},
            'diagnostics': diagnostics,
            'limitations': ['Categories and entrypoints are candidates; verify behavior in source.',
                            'Facts are extracted only for package.json, go.mod and build target candidates.',
                            'Hidden directories except CI, dependencies and build output are excluded.',
                            'No test execution or coverage measurement is performed.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--out')
    args = parser.parse_args()
    try:
        report = scan(args.root)
    except ValueError as exc:
        parser.error(str(exc))
    data = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(data + '\n', encoding='utf-8')
    else:
        print(data)


if __name__ == '__main__':
    main()
