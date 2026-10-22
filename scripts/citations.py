"""Visible source citations shared by writing and evidence validation."""
from pathlib import Path
import re
from urllib.parse import quote, urlsplit
from artifacts import safe_path

LABEL = re.compile(r'(.+):L(\d+)(?:-L(\d+))?')


def citation(root, document, ref):
    import os
    path = safe_path(root, ref['path'])
    target = os.path.relpath(path, safe_path(root, document).parent).replace(os.sep, '/')
    start, end = ref['line'], ref.get('end_line', ref['line'])
    anchor = f'L{start}' + (f'-L{end}' if end != start else '')
    return f"[{ref['path']}:{anchor}]({quote(target, safe='/')}#{anchor})"


def validate_citations(root, document, statement, refs, document_text=None):
    from verify import REF_PAT, local_link_target
    import markdownlinks
    from scanpolicy import SOURCE_EXTS
    root = Path(root).resolve()
    links, text = markdownlinks.analyze(statement, markdownlinks.references(document_text) if document_text is not None else None)
    expected = {(r['path'], r['line'], r.get('end_line', r['line'])) for r in refs}
    actual, errors, linked_labels = set(), [], set()
    evidence_paths = {r['path'] for r in refs}
    for link in links:
        label, target = link.label, link.target
        if link.image: continue
        numbered = LABEL.fullmatch(label)
        local = local_link_target(document, target)
        if local is None:
            if numbered:
                errors.append(f'numbered source citation must be a local link: {label}')
            continue
        try:
            path = (root / local).resolve()
            relative = path.relative_to(root).as_posix()
            if numbered:
                ref, start, end = numbered.groups()
                linked_labels.add(label)
                start, end = int(start), int(end or start)
                if safe_path(root, ref) != path or ref != relative:
                    errors.append(f'citation label/target mismatch: {label}')
                anchor = f'L{start}' + (f'-L{end}' if end != start else '')
                # L1-L1 is also an explicit, valid single-line representation.
                valid_anchors = {anchor, f'L{start}-L{end}'}
                if urlsplit(target.strip('<>')).fragment not in valid_anchors:
                    errors.append(f'citation line anchor missing or mismatched: {label}')
                actual.add((relative, start, end))
            elif relative in evidence_paths or path.suffix.lower() in SOURCE_EXTS:
                errors.append(f'source citation requires a path and line label: {target}')
        except ValueError as exc:
            errors.append(str(exc))
    for match in REF_PAT.finditer(text):
        if match.group(0)[1:-1] not in linked_labels:
            errors.append(f'source citation requires a clickable target: {match.group(0)}')
    for path, start, end in sorted(expected - actual):
        errors.append(f'evidence missing visible citation: {path}:L{start}-L{end}')
    for path, start, end in sorted(actual - expected):
        errors.append(f'visible citation missing matching ledger evidence: {path}:L{start}-L{end}')
    return errors
