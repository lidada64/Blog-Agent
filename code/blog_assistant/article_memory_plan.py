"""Deterministic, bounded block/section/article DAG with contextual reuse keys."""
from dataclasses import dataclass
import hashlib
import json
import re

from .article_memory_build import index_nodes, PARSER_VERSION


@dataclass(frozen=True)
class BuildNode:
    node_id: str
    kind: str
    title: str
    parent_id: str | None
    start_line: int
    end_line: int
    text: str
    dependencies: tuple[str, ...]
    cache_key: str
    context: str


def plan_article(library_id, article_id, version, body, generation, config):
    indexed = index_nodes(article_id, version, body)
    lines = body.splitlines(keepends=True)
    nodes, planned = [], {}
    limit = config['chunk_chars']
    # Context and separators must fit as well. Summaries are validated to this bound.
    fanout = max(2, (limit // 2) // (config['summary_chars'] + 32))
    context = {'article': indexed[0]['title']}
    if len(context['article']) > limit // 4:
        raise ValueError('Article title exceeds context bound')
    for node in indexed[1:]:
        context[node['node_id']] = context[node['parent_id']] + ' / ' + node['title']
        if len(context[node['node_id']]) > limit // 4:
            raise ValueError('Heading ancestry exceeds input bound')

    def add(node_id, kind, title, parent, start, end, text='', deps=()):
        ancestry = context.get(node_id, context.get(parent, context['article']))
        fingerprint = json.dumps([library_id, article_id, generation, PARSER_VERSION, kind,
                                  ancestry, text, [planned[d].cache_key for d in deps]], ensure_ascii=False)
        node = BuildNode(node_id, kind, title, parent, start, end, text, tuple(deps),
                         hashlib.sha256(fingerprint.encode()).hexdigest(), ancestry)
        nodes.append(node)
        planned[node_id] = node
        return node_id

    def blocks(parent, start, end):
        # Keep paragraphs (including contiguous tables) and fenced code intact.
        units, pending, unit_start, fence = [], [], start, None
        for number in range(start, end + 1):
            line = lines[number - 1]
            if not pending:
                unit_start = number
            pending.append(line)
            marker = re.match(r'^\s*(`{3,}|~{3,})', line)
            if marker:
                token = marker[1]
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
            if not fence and not line.strip():
                units.append((unit_start, number, ''.join(pending)))
                pending = []
        if pending:
            units.append((unit_start, end, ''.join(pending)))
        if fence:
            raise ValueError('Unclosed code fence requires source repair')
        result, group, group_start, group_end = [], '', start, start
        body_limit = limit // 2
        for first, last, text in units:
            if len(text) > body_limit:
                raise ValueError('Oversized paragraph/code/table; no silent truncation')
            if group and len(group) + len(text) > body_limit:
                result.append(add(f'{parent}:block:{len(result)}', 'block', '', parent, group_start, group_end, group))
                group = ''
            if not group:
                group_start = first
            group += text
            group_end = last
        if group.strip():
            result.append(add(f'{parent}:block:{len(result)}', 'block', '', parent, group_start, group_end, group))
        return result

    own = {}
    for index, node in enumerate(indexed):
        start = node['start_line']
        # Article owns only preamble; each heading owns until the next heading.
        end = indexed[index + 1]['start_line'] - 1 if index + 1 < len(indexed) else len(lines)
        if index == 0 and len(indexed) == 1:
            end = len(lines)
        own[node['node_id']] = blocks(node['node_id'], start, end) if start <= end else []
    for node in reversed(indexed):
        node_id = node['node_id']
        deps = own[node_id] + [child['node_id'] for child in indexed[1:] if child['parent_id'] == node_id]
        depth = 0
        while len(deps) > fanout:
            merged = []
            for offset in range(0, len(deps), fanout):
                batch = deps[offset:offset + fanout]
                merged.append(add(f'{node_id}:merge:{depth}:{offset}', 'merge', node['title'], node_id,
                                  min(planned[d].start_line for d in batch), max(planned[d].end_line for d in batch), deps=batch))
            deps, depth = merged, depth + 1
        add(node_id, node['kind'], node['title'], node['parent_id'], node['start_line'], node['end_line'], deps=deps)
    return nodes
