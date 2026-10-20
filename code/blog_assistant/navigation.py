"""Bounded navigation metadata, never a substitute for a verified body read."""
import json

from .reference_sections import citation_sections


def navigation(post, max_bytes=4000):
    nodes = citation_sections(post)
    total = len(post['body'].splitlines())
    candidates = nodes or [dict(title=f'Lines {start}–{min(start + 79, total)}',
                               start_line=start, end_line=min(start + 79, total), level=0)
                           for start in range(1, total + 1, 80)]
    result = {'kind': 'navigation_only', 'items': [], 'total_items': len(candidates),
              'truncated': False, 'next_start_line': None}
    for node in candidates:
        item = {key: node[key] for key in ('title', 'start_line', 'end_line', 'level')}
        item['title'] = item['title'][:160]
        if 'section_id' in node:
            item['section_id'] = node['section_id']
        proposed = {**result, 'items': result['items'] + [item]}
        # Reserve room for truncation metadata and a continuation line number.
        if len(result['items']) >= 20 or len(json.dumps(proposed, ensure_ascii=False).encode('utf-8')) > max_bytes - 80:
            result.update(truncated=True, next_start_line=node['start_line'])
            break
        result['items'].append(item)
    return result
