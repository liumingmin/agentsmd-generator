"""Read Markdown link destinations without changing labels or code examples.

Supports inline/image links and full, collapsed and shortcut reference links.
All offsets refer to the original text; rewrites touch destination spans only.
"""
from dataclasses import dataclass
import html
import re


@dataclass(frozen=True)
class Link:
    label: str
    target: str
    start: int
    end: int
    destination_start: int
    destination_end: int
    image: bool = False


def blank(output, start, end):
    output[start:end] = ['\n' if c == '\n' else ' ' for c in output[start:end]]


def escaped(text, i):
    count = 0; i -= 1
    while i >= 0 and text[i] == '\\': count += 1; i -= 1
    return bool(count % 2)


def active_mask(text, keep_comments=False):
    output = list(text)
    masked, offset, fence = ''.join(output), 0, None
    for line in masked.splitlines(keepends=True):
        content = re.sub(r'^(?: {0,3}>[ \t]?)+', '', line)
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', content.rstrip('\r\n'))
        if fence:
            blank(output, offset, offset + len(line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip(): fence = None
        elif marker:
            fence = marker[1]; blank(output, offset, offset + len(line))
        elif content.startswith(('    ', '\t')):
            blank(output, offset, offset + len(line))
        offset += len(line)
    masked, i = ''.join(output), 0
    while i < len(masked):
        if masked[i] != '`' or escaped(masked, i): i += 1; continue
        end = i + 1
        while end < len(masked) and masked[end] == '`': end += 1
        run, search = masked[i:end], end
        while True:
            closing = masked.find(run, search)
            if closing < 0: break
            stop = closing + len(run)
            if (closing == 0 or masked[closing - 1] != '`') and (stop == len(masked) or masked[stop] != '`'):
                blank(output, i, stop); i = stop; break
            search = stop
        if closing < 0: i = end
    masked = ''.join(output)
    if not keep_comments:
        for match in re.finditer(r'<!--.*?(?:-->|\Z)', masked, re.S): blank(output, *match.span())
    return ''.join(output)


def decode(value):
    return html.unescape(re.sub(r'\\([!"#$%&\'()*+,\-./:;<=>?@\[\]\\^_`{|}~])', r'\1', value))


def label_key(value):
    return ' '.join(decode(value).split()).casefold()


def label_end(text, start, limit):
    depth, i = 1, start + 1
    while i < min(limit, start + 1001):
        if text[i] == '\\': i += 2; continue
        if text[i] == '[': depth += 1
        if text[i] == ']':
            depth -= 1
            if depth == 0: return i
        if text.startswith('\n\n', i): return None
        i += 1
    return None


def destination(text, i, limit):
    if i < limit and text[i] == '<':
        start = i + 1; i += 1
        while i < limit:
            if text[i] == '\\': i += 2; continue
            if text[i] in '\r\n<': return None
            if text[i] == '>': return start, i, i + 1
            i += 1
        return None
    start, depth = i, 0
    while i < limit:
        char = text[i]
        if char == '\\': i += 2; continue
        if char.isspace() or (char == ')' and not depth): break
        if char == '<': return None
        if char == '(': depth += 1
        if char == ')': depth -= 1
        i += 1
    if depth or i > limit: return None
    return start, i, i


def whitespace(text, i, limit):
    while i < limit and text[i].isspace(): i += 1
    return i


def title_end(text, i, limit):
    if i >= limit or text[i] not in ('"', "'", '('): return None
    close = ')' if text[i] == '(' else text[i]
    i += 1
    while i < limit:
        if text[i] == '\\': i += 2; continue
        if text[i] == close: return i + 1
        i += 1
    return None


def definition_data(text):
    active, definitions, definition_spans = active_mask(text), {}, []
    # Definitions are block syntax, parsed before uses; first definition wins.
    for match in re.finditer(r'(?m)^ {0,3}\[', active):
        start = match.end() - 1
        close = label_end(active, start, len(active))
        if close is None or active[close + 1:close + 2] != ':': continue
        key = label_key(text[start + 1:close])
        if not key: continue
        i = whitespace(active, close + 2, len(active))
        parsed = destination(active, i, len(active))
        if parsed is None or parsed[0] == parsed[1]: continue
        ds, de, end = parsed
        line_end = active.find('\n', end)
        if line_end < 0: line_end = len(active)
        tail = active[end:line_end].strip()
        if tail:
            ts = whitespace(active, end, line_end)
            te = title_end(active, ts, line_end)
            if te is None or active[te:line_end].strip(): continue
        definitions.setdefault(key, (ds, de, decode(text[ds:de])))
        definition_spans.append((match.start(), line_end))
    visible = list(active)
    for span in definition_spans: blank(visible, *span)
    return definitions, ''.join(visible)


def references(text):
    return {key: value[2] for key, value in definition_data(text)[0].items()}


def analyze(text, references=None):
    definitions, visible = definition_data(text)
    if references:
        definitions.update({key: (-1, -1, target) for key, target in references.items()})
    found = []

    def scan(begin, limit, images_only=False):
        i = begin
        while i < limit:
            if visible[i] == '\\': i += 2; continue
            if visible[i] != '[': i += 1; continue
            close = label_end(visible, i, limit)
            if close is None: i += 1; continue
            image = i > 0 and visible[i - 1] == '!' and not escaped(visible, i - 1)
            if images_only and not image: i += 1; continue
            end, target_span = close + 1, None
            if end < limit and visible[end] == '(':
                ds_start = whitespace(visible, end + 1, limit)
                parsed = destination(visible, ds_start, limit)
                if parsed is not None:
                    ds, de, stop = parsed
                    after = whitespace(visible, stop, limit)
                    if after > stop and after < limit and visible[after] in ('"', "'", '('):
                        title = title_end(visible, after, limit)
                        after = whitespace(visible, title, limit) if title is not None else limit
                    if after < limit and visible[after] == ')' and '\n\n' not in visible[end:after]:
                        target_span, end = (ds, de, decode(text[ds:de])), after + 1
            if target_span is None:
                key = label_key(text[i + 1:close])
                if end < limit and visible[end] == '[':
                    ref_end = label_end(visible, end, limit)
                    if ref_end is not None:
                        key = label_key(text[end + 1:ref_end]) or key
                        if key in definitions: target_span, end = definitions[key], ref_end + 1
                elif key in definitions: target_span = definitions[key]
            if target_span is None: i += 1; continue
            ds, de, target = target_span
            found.append(Link(decode(text[i + 1:close]), target, i - int(image), end, ds, de, image))
            scan(i + 1, close, images_only=True)
            i = end
    scan(0, len(visible))
    return found, visible


def links(text):
    return analyze(text)[0]


def rewrite(text, transform):
    edits = {}
    for link in links(text):
        key = link.destination_start, link.destination_end
        if key not in edits:
            target = transform(link.target)
            if target is not None and target != link.target: edits[key] = target
    for (start, end), value in sorted(edits.items(), reverse=True): text = text[:start] + value + text[end:]
    return text
