#!/usr/bin/env python3
"""Run an explicitly supplied test command and bind its result to selected inputs."""
import argparse
import copy
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
from artifacts import file_map, load_reports, read_json, safe_path
from scanpolicy import digest, select_files, current_selection
from schemas import mapping_errors, require
from snapshots import atomic_json


def snapshot(root, reports):
    scope = reports[0].get('scope')
    selected = current_selection(root, scope) if scope else select_files(root)
    if selected['diagnostics']:
        raise ValueError('test inputs could not be fully scanned; inspect selection diagnostics')
    paths = list(selected['files'])
    if (Path(root) / '.agentsmd/config.json').exists(): paths.append('.agentsmd/config.json')
    return {'scope': selected['scope'], 'source_hashes': {
        path: digest(safe_path(root, path).read_bytes()) for path in paths}}


def freshness(execution, current):
    if 'source_hashes' not in execution or 'scope' not in execution:
        return ['test execution snapshot missing; result is historical, rerun the explicit test command']
    if execution.get('inputs_unchanged') is False or any(execution.get(key) != current[key] for key in ('scope', 'source_hashes')):
        return ['test execution stale; result is historical for different inputs, rerun or declare not_run for this version']
    return []


def run(root, mapping, reports, module_ids, command, cwd='.'):
    require(mapping_errors(mapping))
    root = Path(root).resolve()
    from guidewrites import check_pending
    check_pending(root)
    if Path(mapping['root']).resolve() != root: raise ValueError('module map root mismatch')
    by_id = {m['id']: m for m in mapping['modules']}
    if len(by_id) != len(mapping['modules']): raise ValueError('duplicate module id')
    if not module_ids or any(mid not in by_id for mid in module_ids):
        raise ValueError('specify existing --module IDs for the test result')
    if not command: raise ValueError('supply a test command after --')
    working = safe_path(root, cwd)
    if not working.is_dir(): raise ValueError('test cwd must be a project directory')
    before = snapshot(root, reports)
    if reports[0].get('scope') and before['scope'] != reports[0]['scope']:
        raise ValueError('test selection differs from analysis; reanalyze before running')
    for path, file in file_map(reports).items():
        if file.get('content_hash') and before['source_hashes'].get(path) != file['content_hash']:
            raise ValueError(f'test source index stale: {path}; reanalyze before running')
    started = datetime.now(timezone.utc).isoformat()
    # Argument vector, no implicit shell. Output stays visible to the user.
    result = subprocess.run(command, cwd=working)
    after = snapshot(root, reports)
    execution = {'command': shlex.join(command), 'argv': command, 'cwd': working.relative_to(root).as_posix(),
                 'timestamp': started, 'exit_code': result.returncode, **before,
                 'inputs_unchanged': before == after}
    output = copy.deepcopy(mapping)
    for module in output['modules']:
        if module['id'] in module_ids:
            module.setdefault('review', {})['test'] = {
                'status': 'passed' if result.returncode == 0 else 'failed',
                'reason': f'Explicit command exited with {result.returncode}; bound to selected source, test and configuration inputs',
                'execution': copy.deepcopy(execution)}
    return output, result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--map', dest='mapping')
    parser.add_argument('--module', action='append', required=True)
    parser.add_argument('--cwd', default='.')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    path = Path(args.mapping) if args.mapping else root / '.agentsmd/modules.json'
    try:
        original = path.read_bytes()
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        output, status = run(root, read_json(path), load_reports(root), args.module, command, args.cwd)
        if path.read_bytes() != original:
            raise ValueError('module map changed while tests ran; result was not written')
        atomic_json(path, output)
        print(f'Test result recorded for {len(set(args.module))} module(s); input fingerprints captured at execution')
        raise SystemExit(0 if status == 0 else 1)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
