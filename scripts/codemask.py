"""Conservative lexical masks for reading hints, preserving source line numbers.

Literal values become inert expressions; comments disappear. Unsupported or
unterminated syntax yields no hints rather than searching unparsed text.
"""
import io
from pathlib import Path
import re
import token
import tokenize

RAW = re.compile(r'(?:br|r)(#+)?"')
CPP = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\n]{0,16})\(')


def erase(output, start, end, literal=False):
    output[start:end] = ['\n' if c == '\n' else ' ' for c in output[start:end]]
    if literal and start < end and output[start] != '\n': output[start] = '0'


def python_mask(text):
    output = list(text)
    starts, total = [], 0
    for line in text.splitlines(keepends=True):
        starts.append(total); total += len(line)
    starts.append(total)
    try:
        for item in tokenize.generate_tokens(io.StringIO(text).readline):
            if item.type not in (token.STRING, tokenize.COMMENT): continue
            start = starts[item.start[0] - 1] + item.start[1]
            end = starts[item.end[0] - 1] + item.end[1]
            erase(output, start, end, item.type == token.STRING)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return ''.join(output)


def quoted_end(text, start, quote, raw=False):
    i = start + 1
    while i < len(text):
        if text[i] == quote: return i + 1
        if text[i] == '\\' and not raw: i += 2
        else: i += 1
    return None


def template_end(text, start):
    """Ignore an entire template, including nested interpolations, conservatively."""
    i, depth = start + 1, 0
    while i < len(text):
        char = text[i]
        if char == '\\': i += 2; continue
        if not depth and char == '`': return i + 1
        if text.startswith('${', i): depth += 1; i += 2; continue
        if depth:
            if text.startswith('//', i):
                i = text.find('\n', i)
                if i < 0: return None
                continue
            if text.startswith('/*', i):
                end = text.find('*/', i + 2)
                if end < 0: return None
                i = end + 2; continue
            if char in ('"', "'", '`'):
                end = template_end(text, i) if char == '`' else quoted_end(text, i, char)
                if end is None: return None
                i = end; continue
            if char == '{': depth += 1
            if char == '}': depth -= 1
        i += 1
    return None


def c_mask(text, javascript=False, go=False):
    output, i, expression = list(text), 0, True
    while i < len(text):
        start, char = i, text[i]
        if text.startswith('//', i):
            end = text.find('\n', i)
            if end < 0: end = len(text)
            erase(output, i, end); i = end; continue
        if text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0: return None
            erase(output, i, end + 2); i = end + 2; continue
        # Rust/C++ raw string prefixes and quoted values are skipped as one token.
        raw = RAW.match(text, i)
        cpp = CPP.match(text, i)
        if raw:
            terminator = '"' + (raw.group(1) or '')
            end = text.find(terminator, raw.end())
            if end < 0: return None
            i = end + len(terminator); erase(output, start, i, True); expression = False; continue
        if cpp:
            terminator = ')' + cpp.group(1) + '"'
            end = text.find(terminator, cpp.end())
            if end < 0: return None
            i = end + len(terminator); erase(output, start, i, True); expression = False; continue
        if char in ('"', "'", '`'):
            end = template_end(text, i) if javascript and char == '`' else quoted_end(text, i, char, go and char == '`')
            if end is None: return None
            erase(output, start, end, True); i, expression = end, False; continue
        if javascript and char == '/' and expression:
            # Regex values are literals too. Track character classes and escapes.
            i, bracket = i + 1, False
            while i < len(text):
                if text[i] == '\n': return None
                if text[i] == '\\': i += 2; continue
                if text[i] == '[': bracket = True
                elif text[i] == ']': bracket = False
                elif text[i] == '/' and not bracket: break
                i += 1
            if i == len(text): return None
            i += 1
            while i < len(text) and text[i].isalpha(): i += 1
            erase(output, start, i, True); expression = False; continue
        if char.isalpha() or char in '_$':
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] in '_$'): i += 1
            expression = text[start:i] in ('return', 'throw', 'case', 'yield', 'typeof', 'void', 'delete', 'in', 'of')
            continue
        if not char.isspace(): expression = char in '=([{,:;!?&|+-*%>'
        i += 1
    return ''.join(output)


def code_mask(path, text, candidate_exclusions=None, syntax_valid=True):
    suffix = Path(path).suffix.lower()
    if suffix == '.py': return python_mask(text)
    js = suffix in {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.mts', '.cts'}
    if js:
        if candidate_exclusions is None or not syntax_valid: return None
        # TypeScript offsets count UTF-16 code units, Python counts code points.
        offsets, units = {0: 0}, 0
        for i, char in enumerate(text):
            units += 2 if ord(char) > 0xffff else 1
            offsets[units] = i + 1
        output, last = list(text), 0
        for span in candidate_exclusions:
            if not isinstance(span, dict): return None
            start, end = span.get('start'), span.get('end')
            if type(span.get('literal')) is not bool: return None
            if type(start) is not int or type(end) is not int or start not in offsets or end not in offsets or not last <= start < end:
                return None
            erase(output, offsets[start], offsets[end], span['literal'])
            last = end
        return ''.join(output)
    if js or suffix in {'.go', '.c', '.h', '.cc', '.cpp', '.hpp', '.java', '.kt', '.rs', '.cs'}:
        return c_mask(text, javascript=js, go=suffix == '.go')
    return None
