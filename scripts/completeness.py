"""Reject unfinished generated scaffolding without banning documented uncertainty."""
import re
from markdownlinks import active_mask, analyze, blank

# Exact legacy prompts, not general words such as TODO/unknown/待核实.
LEGACY = ('待核实：根据关键实现补充流程。', '待核实：记录命令来源、前提及真实执行状态。',
          '待核实：阅读双方调用位置后补充。', '待核实：尚未完成实现阅读。',
          '待核实：根据项目入口说明用途与使用者。', '待核实：根据归属实现说明职责与边界。',
          '待核实：根据具体能力说明触发、处理和结果。',
          '尚未登记的主要能力', '以下仅为阅读候选，不表示已确认。')
MARKER = re.compile(r'<!--\s*agentsmd:(?:placeholder\b|endplaceholder\b|draft-claim\b)[^>]*-->')

# Titles are aliases, not a prescribed document outline. Custom headings can
# declare their purpose with an agentsmd:section marker inside the section.
SECTIONS = {
    'purpose': ('项目定位', '项目概览', '项目简介', '概述', 'overview', 'purpose'),
    'architecture': ('架构与数据流', '架构', '数据流', 'architecture', 'data flow'),
    'commands': ('构建/运行/测试', '构建与运行', '构建与测试', '运行指南', 'commands', 'build and test', 'getting started'),
    'responsibility': ('职责与目录', '职责', '模块职责', '模块概述', 'responsibilities', 'module overview'),
    'behavior': ('功能点清单', '主要功能', '核心行为', '功能与流程', 'features', 'behavior', 'capabilities'),
    'collaboration': ('依赖与协作', '依赖', '模块协作', 'dependencies', 'collaboration'),
    'tests': ('测试与验证', '测试', '验证', 'tests', 'testing', 'validation'),
}


def has_section(text, key):
    text = re.sub(r'<!-- agentsmd:draft-navigation -->.*?<!-- agentsmd:enddraft-navigation -->', '', text, flags=re.S)
    text = active_mask(text, keep_comments=True)
    if re.search(r'<!--\s*agentsmd:section\s+' + re.escape(key) + r'\s*-->', text): return True
    titles = {t.strip().rstrip('#').strip().casefold() for t in re.findall(r'(?m)^#{1,6}\s+(.+)$', text)}
    return bool(titles & set(SECTIONS[key]))


def section_errors(text, root=False):
    """Check for authored explanations, never claim to validate their semantics."""
    # Navigation and tables generated from indexes cannot stand in for prose.
    text = re.sub(r'<!-- agentsmd:draft-navigation -->.*?<!-- agentsmd:enddraft-navigation -->', '', text, flags=re.S)
    # Parse definitions across the whole document before splitting sections.
    # Use aligned masks: real markers declare purpose, links/code are not prose.
    links, visible = analyze(text)
    narrative = list(visible)
    for link in links: blank(narrative, link.start, link.end)
    narrative = ''.join(narrative)
    text = active_mask(text, keep_comments=True)
    chunks, level, title, body = [], 0, '', []
    prose_body = []
    for line, prose_line in zip(text.splitlines(), narrative.splitlines()):
        heading = re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line)
        if heading:
            chunks.append((level, title, '\n'.join(body), '\n'.join(prose_body)))
            level = len(line) - len(line.lstrip('#'))
            title, body, prose_body = heading.group(1).strip().casefold(), [], []
        else:
            body.append(line)
            prose_body.append(prose_line)
    chunks.append((level, title, '\n'.join(body), '\n'.join(prose_body)))
    covered, parents = set(), []
    for level, title, body, prose in chunks:
        parents = [(depth, sections) for depth, sections in parents if depth < level]
        declared = set(re.findall(r'<!--\s*agentsmd:section\s+([a-z]+)\s*-->', body))
        declared.update(key for key, aliases in SECTIONS.items() if title in aliases)
        parents.append((level, declared))
        declared = set().union(*(sections for _, sections in parents))
        prose = '\n'.join(line for line in prose.splitlines() if not line.lstrip().startswith('|'))
        prose = re.sub(r'[\s#*>_`~\-:：;；.。]+', '', prose)
        if prose and prose.casefold() not in ('todo', '待补充', '待核实', 'unknown', 'n/a', 'na') and not unfinished(body):
            covered.update(declared)
    required = ('purpose', 'architecture', 'commands') if root else ('responsibility', 'behavior', 'collaboration', 'tests')
    return [f'document section missing or empty: {key}; add an explanation or a reason for non-applicability'
            for key in required if key not in covered]


def unfinished(text):
    text = active_mask(text, keep_comments=True)
    issues = []
    for number, line in enumerate(text.splitlines(), 1):
        if MARKER.search(line) or any(phrase in line for phrase in LEGACY):
            issues.append(f'unfinished draft placeholder at line {number}; replace with reviewed content')
    return issues
