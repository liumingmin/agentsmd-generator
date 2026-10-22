"""Find aspect-specific reading hints; lexical matches never confirm a claim."""
from pathlib import Path
import re
from artifacts import safe_path
from codemask import code_mask
from scanpolicy import digest

PATTERNS = {
    'input': re.compile(r'\b(?:assert|validate\w*|check\w*|parse\w*|decode\w*)\b|\bif\b.*(?:\blen\b|\bnull\b|\bNone\b|\bnil\b|required|input|request|value|param)', re.I),
    'success': re.compile(r'\breturn\s+(?!None\b|null\b|nil\b|err\w*\b|new\s+Error\b)\S|\b(?:save|insert|update|publish|send|write|commit)\w*\s*\(', re.I),
    'failure': re.compile(r'\b(?:raise|throw|except|catch|panic)\b|\breturn\b.*\b(?:err\w*|Error|nil,|None,)\b|\bif\s+err\b', re.I),
    'configuration': re.compile(r'\b(?:getenv|environ|LookupEnv|ReadConfig|load_config)\b|\b(?:config|cfg|settings|process\.env)\s*[.\[]', re.I),
}


def candidate_refs(root, module, files, aspect, capability=None, cache=None):
    pattern = PATTERNS[aspect]
    preferred = set(module.get('review', {}).get('implementation', []))
    tokens = set(re.findall(r'[a-zA-Z][a-zA-Z0-9]+', (capability or {}).get('id', '') + ' ' + (capability or {}).get('title', '')))
    matches = []
    cache = {} if cache is None else cache
    for path in module.get('files', []):
        file = files.get(path, {})
        if file.get('is_test') or file.get('generated'): continue
        if path not in cache:
            try:
                raw = safe_path(root, path).read_bytes()
                masked = code_mask(path, raw.decode('utf-8'), file.get('candidate_exclusions'), file.get('candidate_syntax_valid', False)) if not file.get('content_hash') or digest(raw) == file['content_hash'] else None
                cache[path] = masked.splitlines() if masked is not None else []
            except (OSError, UnicodeError): cache[path] = []
        lines = cache[path]
        for number, line in enumerate(lines, 1):
            if not pattern.search(line): continue
            symbol = next((s for s in file.get('symbols') or [] if s['line'] <= number <= s['end_line']), None)
            score = (2 if path in preferred else 0) + sum(token.casefold() in ((symbol or {}).get('name', '') + ' ' + path).casefold() for token in tokens)
            ref = {'role': 'implementation', 'path': path, 'line': number,
                   'end_line': min(number + 2, (symbol or {}).get('end_line', len(lines)), len(lines))}
            if symbol and symbol.get('content_hash') and symbol.get('id'):
                ref['symbol_id'] = symbol['id']
            matches.append((-score, path, number, ref))
    # Keep the reading queue short. No generic first-file/first-symbol fallback.
    return [item[3] for item in sorted(matches, key=lambda item: item[:3])[:3]]
