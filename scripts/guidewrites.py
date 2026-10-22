"""Recoverable guide batches: durable preimages, staged outputs and one journal.

Readers can see partial updates during a batch. On interruption the journal
blocks new batches/delivery; recovery rolls back without overwriting user edits.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
from guidepaths import contained_path, guide_path
from scanpolicy import digest
from snapshots import atomic_json, raw_json, writer_lock


def store(root):
    probe = contained_path(root, '.agentsmd/guide-write/active.json')
    contained_path(root, '.agentsmd/guide-write/.publish.lock')
    contained_path(root, '.agentsmd/guide-write/last.json')
    return probe.parent


def target(root, relative):
    if relative in ('.agentsmd/modules.json', '.agentsmd/evidence.json'):
        return contained_path(root, relative)
    if Path(relative).name in ('modules.draft.json', 'evidence.draft.json'):
        if Path(relative).parent != Path('.agentsmd') and Path(relative).parts[:2] != ('.agentsmd', 'drafts'):
            raise ValueError(f'invalid batch artifact target: {relative}')
        return contained_path(root, relative)
    return guide_path(root, relative)


def pending(root):
    path = store(root) / 'active.json'
    return path.exists()


def check_pending(root):
    if pending(root):
        raise ValueError('unfinished guide batch; run draft.py --root <project> --recover before generation or delivery')


def replace_bytes(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def read_bytes(path):
    return path.read_bytes() if path.exists() else None


def hashed(content):
    return digest(content) if content is not None else None


def load_batch(root, directory):
    journal = raw_json(directory / 'active.json')
    if not isinstance(journal, dict) or journal.get('version') != 1 or journal.get('root') != str(Path(root).resolve()):
        raise ValueError('invalid guide recovery journal root')
    sid = journal.get('id')
    if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-f]{32}', sid):
        raise ValueError('invalid guide recovery batch id')
    if journal.get('state') not in ('applying', 'committed', 'rolled_back') or not isinstance(journal.get('entries'), list):
        raise ValueError('invalid guide recovery journal state/entries')
    stage = directory / sid
    entries, seen = [], set()
    for i, item in enumerate(journal['entries']):
        if not isinstance(item, dict) or 'before_hash' not in item or not isinstance(item.get('path'), str) or type(item.get('mode')) is not int or not 0 <= item['mode'] <= 0o7777:
            raise ValueError('invalid guide recovery journal entry')
        path = target(root, item['path'])
        alias = unicodedata.normalize('NFC', str(path)).casefold()
        if alias in seen: raise ValueError('duplicate guide recovery target')
        seen.add(alias)
        values = []
        for suffix in ('before', 'after'):
            expected = item.get(suffix + '_hash')
            if suffix == 'before' and expected is None:
                values.append(None)
                continue
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise ValueError('invalid guide recovery checksum')
            blob = contained_path(root, (stage / f'{i}.{suffix}').relative_to(root).as_posix()).read_bytes()
            if digest(blob) != expected: raise ValueError('guide recovery backup checksum mismatch')
            values.append(blob)
        entries.append((path, item, *values))
    return journal, stage, entries


def finish(directory, journal, stage):
    atomic_json(directory / 'last.json', journal)
    (directory / 'active.json').unlink()
    shutil.rmtree(stage)


def recover_locked(root, directory):
    if not (directory / 'active.json').exists(): return False
    journal, stage, entries = load_batch(root, directory)
    if journal['state'] == 'applying':
        # Inspect every target before restoring any file. User edits are conflicts.
        for path, item, before, after in entries:
            if read_bytes(path) not in (before, after):
                raise ValueError(f'guide recovery conflict: {item["path"]}; preserve the edited file elsewhere and restore its before/after version before retrying --recover')
        for path, item, before, after in reversed(entries):
            target(root, item['path'])
            if read_bytes(path) == before: continue
            if read_bytes(path) != after: raise ValueError(f'guide recovery concurrent edit: {item["path"]}')
            if before is None: path.unlink()
            else: replace_bytes(path, before, item['mode'])
        journal['state'] = 'rolled_back'
        atomic_json(directory / 'active.json', journal)
    finish(directory, journal, stage)
    return True


def recover(root):
    root = Path(root).resolve()
    directory = store(root)
    if not directory.exists(): return False
    with writer_lock(directory):
        return recover_locked(root, directory)


def apply(root, writes, originals=None, after_replace=None, dependencies=None):
    """On ordinary failure roll back; process termination leaves a recoverable batch."""
    root = Path(root).resolve()
    directory = store(root)
    with writer_lock(directory):
        check_pending(root)
        for path, expected in (dependencies or {}).items():
            if read_bytes(Path(path)) != expected: raise ValueError(f'preview input changed: {path}; rerun promotion')
        journal = {'version': 1, 'root': str(root), 'id': uuid.uuid4().hex, 'state': 'applying', 'entries': []}
        stage = directory / journal['id']
        planned = []
        aliases = set()
        for relative, content in writes.items():
            path = target(root, relative)
            alias = unicodedata.normalize('NFC', str(path)).casefold()
            if alias in aliases: raise ValueError(f'duplicate batch target: {relative}')
            aliases.add(alias)
            before = read_bytes(path)
            if originals is not None and relative in originals and before != originals[relative]:
                raise ValueError(f'guide changed during generation: {relative}; rerun')
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            planned.append((relative, path, before, content, mode))
        stage.mkdir()
        try:
            for i, (relative, path, before, after, mode) in enumerate(planned):
                if before is not None: replace_bytes(stage / f'{i}.before', before, 0o600)
                replace_bytes(stage / f'{i}.after', after, 0o600)
                journal['entries'].append({'path': relative, 'before_hash': hashed(before), 'after_hash': digest(after), 'mode': mode})
            for relative, path, before, after, mode in planned:
                target(root, relative)
                if read_bytes(path) != before: raise ValueError(f'guide changed while staging: {relative}')
            for path, expected in (dependencies or {}).items():
                if read_bytes(Path(path)) != expected: raise ValueError(f'preview input changed while staging: {path}; rerun promotion')
            atomic_json(directory / 'active.json', journal)
            for i, (relative, path, before, after, mode) in enumerate(planned):
                target(root, relative)
                if read_bytes(path) != before: raise ValueError(f'guide changed during batch: {relative}')
                replace_bytes(path, after, mode)
                if after_replace: after_replace(i, path)
            journal['state'] = 'committed'
            atomic_json(directory / 'active.json', journal)
            finish(directory, journal, stage)
        except Exception:
            if (directory / 'active.json').exists(): recover_locked(root, directory)
            elif stage.exists(): shutil.rmtree(stage)
            raise
        # BaseException deliberately leaves the journal, as a killed process would.


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
