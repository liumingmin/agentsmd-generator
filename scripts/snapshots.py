"""Immutable analysis generations published by one atomic manifest replacement."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

REPORT_NAMES = ('selection.json', 'context.json', 'index.json', 'ast.json', 'module-candidates.json')


def raw_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def manifest(out):
    path = Path(out) / 'current.json'
    data = raw_json(path) if path.is_file() else None
    if not isinstance(data, dict) or 'agentsmd_generation' not in data: return None
    from schemas import require, snapshot_errors
    require(snapshot_errors(data, 'current', current=True))
    return data


def resolve(out, name, pinned=None):
    out = Path(out).resolve()
    selected = pinned if pinned is not None else manifest(out)
    if selected is None:
        return out / name
    if name.endswith('.old.json'):
        selected = selected.get('previous') or {}
        name = name.replace('.old.json', '.json')
    relative = selected.get('artifacts', {}).get(name)
    if relative is None:
        raise FileNotFoundError(f'{name} is not present in the selected analysis generation')
    path = (out / relative).resolve()
    if not path.is_relative_to(out / 'snapshots'):
        raise ValueError('snapshot path is outside the generation store')
    return path


def read_report(path, pinned=None):
    path = Path(path)
    expected, selected = None, None
    if path.name in REPORT_NAMES or path.name.replace('.old.json', '.json') in REPORT_NAMES:
        current = pinned if pinned is not None else manifest(path.parent)
        if current:
            selected = current.get('previous') if path.name.endswith('.old.json') else current
            name = path.name.replace('.old.json', '.json')
            expected = (selected or {}).get('hashes', {}).get(name)
            path = resolve(path.parent, path.name, current)
    bundle = path.parent / 'bundle.json'
    if selected is None and path.parent.parent.name == 'snapshots' and bundle.is_file():
        selected = raw_json(bundle)
        from schemas import require, snapshot_errors
        require(snapshot_errors(selected, 'bundle', bundle=True))
        if path.name not in selected['hashes']:
            raise ValueError(f'{path}: artifact missing from bundle checksums')
        expected = selected.get('hashes', {}).get(path.name)
    raw = path.read_bytes()
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f'{path}: immutable snapshot checksum mismatch')
    value = json.loads(raw)
    if isinstance(value, dict) and 'agentsmd_snapshot_reference' in value:
        raise ValueError(f'{path}: missing current.json; generation reference cannot be read')
    if selected and (not isinstance(value, dict) or value.get('snapshot_id') != selected.get('snapshot_id')):
        raise ValueError(f'{path}: generation identity mismatch')
    return value


def paths(out, names=('context.json', 'ast.json', 'index.json'), pinned=None):
    out = Path(out)
    current = pinned if pinned is not None else manifest(out)
    if current:
        return [out / name for name in names if name in current['artifacts']]
    return [out / name for name in names if (out / name).is_file()]


@contextmanager
def writer_lock(out):
    """Kernel locks release on process termination; no stale PID lock recovery needed."""
    out.mkdir(parents=True, exist_ok=True)
    with (out / '.publish.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            stream.write(b'0')
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError('another analysis publisher is running') from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('another analysis publisher is running') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def stage_generation(out, values):
    store = out / 'snapshots'
    store.mkdir(exist_ok=True)
    sid = uuid.uuid4().hex
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=store))
    try:
        hashes = {}
        for name, value in values.items():
            if name not in REPORT_NAMES:
                raise ValueError(f'unsupported analysis artifact: {name}')
            value = {**value, 'snapshot_id': sid}
            atomic_json(staging / name, value)
            hashes[name] = hashlib.sha256((staging / name).read_bytes()).hexdigest()
        atomic_json(staging / 'bundle.json', {'snapshot_id': sid, 'hashes': hashes})
        final = store / sid
        os.replace(staging, final)
        return {'snapshot_id': sid,
                'artifacts': {name: f'snapshots/{sid}/{name}' for name in values},
                'hashes': hashes}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def publish(out, values, before_switch=None):
    """A failed publication leaves the entire previous bundle selected."""
    out = Path(out).resolve()
    with writer_lock(out):
        previous = manifest(out)
        if previous:
            previous = {k: v for k, v in previous.items() if k != 'previous'}
        else:
            legacy = {name: raw_json(out / name) for name in REPORT_NAMES
                      if (out / name).is_file()}
            previous = stage_generation(out, legacy) if legacy else None
        current = {'schema_version': 1, 'agentsmd_generation': True, **stage_generation(out, values), 'previous': previous}
        if before_switch:
            before_switch(current)
        atomic_json(out / 'current.json', current)
        # References are convenience entrypoints, never the selected data. Install
        # them after switching so failed legacy migration cannot corrupt old files.
        notices = []
        for name in sorted(set(values) | set((previous or {}).get('artifacts', {}))):
            try:
                if name in values:
                    atomic_json(out / name, {'agentsmd_snapshot_reference': name})
                if previous and name in previous['artifacts']:
                    atomic_json(out / name.replace('.json', '.old.json'), {'agentsmd_snapshot_reference': name})
            except OSError as exc:
                notices.append(f'Published generation, but could not install logical reference {name}: {exc}')
        return {**current, 'reference_warnings': notices}


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['list', 'path', 'show'])
    ap.add_argument('--dir', default='.agentsmd')
    ap.add_argument('--artifact', default='index.json')
    args = ap.parse_args()
    try:
        current = manifest(args.dir)
        if args.command == 'list':
            print(json.dumps(current or {'mode': 'legacy', 'files': [str(p) for p in paths(args.dir)]}, ensure_ascii=False, indent=2))
        elif args.command == 'path':
            print(resolve(args.dir, args.artifact, current))
        else:
            print(json.dumps(read_report(Path(args.dir) / args.artifact, current), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    main()
