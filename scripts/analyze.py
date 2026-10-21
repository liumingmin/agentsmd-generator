#!/usr/bin/env python3
"""Analyze one immutable file selection; preserve review artifacts and old snapshots."""
import argparse
import os
import shutil
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
        index = codeindex.scan(root, selection, js_parser=args.js_parser, typescript=args.typescript, node=args.node)
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
            out.mkdir(parents=True, exist_ok=True)
            for name in ('selection.json', 'context.json', 'index.json', 'ast.json', 'module-candidates.json'):
                target = out / name
                if target.exists():
                    shutil.copy2(target, out / name.replace('.json', '.old.json'))
                if (stage / name).exists():
                    os.replace(stage / name, target) if os.stat(stage).st_dev == os.stat(out).st_dev else shutil.copy2(stage / name, target)
                elif target.exists():
                    target.unlink()  # Do not leave a previous Go report masquerading as current.
        print(f'Analysis: {out}; {len(selection["files"])} selected assets')
        for report in reports:
            print(f"  {report['stats']['file_count']} files; {len(report.get('diagnostics', []))} diagnostics")
            for notice in report.get('notices', []): print('  NOTICE ' + notice)
        print('Review module-candidates.json; modules.json and evidence.json are never overwritten.')
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    main()
