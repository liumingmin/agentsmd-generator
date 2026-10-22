#!/usr/bin/env python3
"""Analyze one immutable file selection; preserve review artifacts and old snapshots."""
import argparse
import os
import subprocess
import tempfile
from pathlib import Path
import codeindex
import projectcontext
import modules
from artifacts import write_json
from scanpolicy import add_arguments, from_args


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', required=True)
    ap.add_argument('--out-dir', help='default <root>/.agentsmd')
    ap.add_argument('--goast', help='prebuilt analyzer; otherwise build in a temporary directory')
    ap.add_argument('--skip-go', action='store_true', help='explicitly omit Go; source ownership still includes Go')
    ap.add_argument('--max-files', type=int, default=64)
    ap.add_argument('--max-symbols', type=int, default=500)
    add_arguments(ap)
    codeindex.parser_arguments(ap)
    args = ap.parse_args()
    root = Path(args.root).resolve()
    out = Path(args.out_dir).resolve() if args.out_dir else root / '.agentsmd'
    if out.is_relative_to(root) and out != root / '.agentsmd':
        ap.error('in-project output must be .agentsmd (prevents scanning previous reports)')
    try:
        selection = from_args(args)
        context = projectcontext.scan(root, selection=selection)
        index = codeindex.scan(root, selection, js_parser=args.js_parser, typescript=args.typescript, node=args.node, tsconfig=args.tsconfig)
        reports = [context, index]
        # Stage every report first: failed parses/builds never partially replace the baseline.
        with tempfile.TemporaryDirectory(prefix='agentsmd-analysis-') as temporary:
            stage = Path(temporary)
            write_json(stage / 'selection.json', selection)
            has_go = not args.skip_go and any(p.endswith('.go') for p in selection['files'])
            if has_go:
                binary = args.goast
                if not binary:
                    binary = str(stage / ('goast.exe' if os.name == 'nt' else 'goast'))
                    subprocess.run(['go', 'build', '-o', binary, '.'], cwd=Path(__file__).parent / 'goast', check=True)
                subprocess.run([binary, '-root', str(root), '-selection', str(stage / 'selection.json'),
                                '-out', str(stage / 'ast.json')], check=True)
                from artifacts import read_json
                reports.append(read_json(stage / 'ast.json'))
            write_json(stage / 'context.json', context)
            write_json(stage / 'index.json', index)
            write_json(stage / 'module-candidates.json', modules.plan(root, reports, args.max_files, args.max_symbols))
            from snapshots import publish
            from artifacts import read_json
            values = {path.name: read_json(path) for path in stage.iterdir() if path.suffix == '.json'}
            from scanpolicy import digest
            skill_scripts = Path(__file__).parent
            implementation = ['analyze.py', 'artifacts.py', 'snapshots.py', 'codeindex.py', 'jsindex.cjs',
                              'scanpolicy.py', 'projectcontext.py', 'modules.py', 'goast/main.go']
            revision = digest(b'\n'.join(name.encode() + b':' + (skill_scripts / name).read_bytes() for name in implementation))
            for value in values.values():
                value['analysis_revision'] = revision
            # Validate source contents and selection again before publishing a coherent bundle.
            from artifacts import file_map
            from scanpolicy import digest, current_selection
            for path, file in file_map(reports).items():
                if file.get('content_hash') and digest((root / path).read_bytes()) != file['content_hash']:
                    raise ValueError(f'source changed during analysis: {path}; rerun')
            if current_selection(root, selection['scope'])['scope'] != selection['scope']:
                raise ValueError('file selection changed during analysis; rerun')
            generation = publish(out, values)
        print(f'Analysis: {out} (generation {generation["snapshot_id"]}); {len(selection["files"])} selected assets')
        for warning in generation.get('reference_warnings', []):
            print('  WARN ' + warning)
        for report in reports:
            print(f"  {report['stats']['file_count']} files; {len(report.get('diagnostics', []))} diagnostics")
            for notice in report.get('notices', []): print('  NOTICE ' + notice)
        print('Review module-candidates.json; modules.json and evidence.json are never overwritten.')
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    main()
