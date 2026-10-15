"""Version-bound citation subtrees over visible reads, never search snippets."""
import hashlib
import re

from .citations import read_lines
from .sections import headings

REFERENCE_VERSION = "2026-10-15.14"


def normalize_title(title):
    """Only peel presentation wrappers; no fuzzy spelling or semantic matches."""
    title = re.sub(r"^#{1,6}\s+", "", title.strip())
    title = re.sub(r"\s+#+$", "", title).strip()
    for _ in range(4):
        previous = title
        for wrapper in ('**', '__', '*', '_', '`'):
            if len(title) > len(wrapper) * 2 and title.startswith(wrapper) and title.endswith(wrapper):
                title = title[len(wrapper):-len(wrapper)].strip()
                break
        if title == previous:
            break
    return title


def citation_sections(post):
    nodes = headings(post['body'])
    total_lines = len(post['body'].splitlines())
    stack = []
    for node in nodes:
        while stack and stack[-1]['level'] >= node['level']:
            stack.pop()['end_line'] = node['start_line'] - 1
        identity = f"{post['post_id']}:{post['version']}:{node['start_line']}"
        node.update(section_id='@s-' + hashlib.sha256(identity.encode()).hexdigest()[:16],
                    title=normalize_title(node['title']),
                    parent_id=stack[-1]['section_id'] if stack else None,
                    end_line=total_lines)
        stack.append(node)
    return nodes


def line_ranges(numbers):
    ranges = []
    for number in sorted(numbers):
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return ranges


class ReferenceCatalog:
    def __init__(self, calls, posts):
        self.indexed = read_lines(calls)
        self.posts = posts
        self.snapshots = {}

    def source(self, post_id):
        if post_id not in self.snapshots:
            try:
                post = self.posts.snapshot(post_id)
            except (OSError, UnicodeError, ValueError, PermissionError) as exc:
                raise ValueError('source_unavailable') from exc
            read = self.indexed.get((post_id, post['version']), {})
            if not read:
                reason = 'stale_source' if any(p == post_id for p, _ in self.indexed) else 'source_not_visible'
                raise ValueError(reason)
            lines = post['body'].splitlines()
            if any(n < 1 or n > len(lines) or lines[n - 1] != line for n, line in read.items()):
                raise ValueError('read_content_mismatch')
            self.snapshots[post_id] = (post, read, citation_sections(post))
        return self.snapshots[post_id]

    @staticmethod
    def body_lines(post, nodes, start=1, end=None):
        titles = {n['start_line'] for n in nodes}
        lines = post['body'].splitlines()
        end = end if end is not None else len(lines)
        return [n for n, line in enumerate(lines, 1)
                if start <= n <= end and n not in titles
                and line.strip() and not re.fullmatch(r'\s*(?:`{3,}|~{3,})\s*', line)]

    def resolve(self, post_id, section):
        post, read, nodes = self.source(post_id)
        node = None
        if section is not None:
            matches = ([n for n in nodes if n['section_id'] == section] if section.startswith('@s-') else
                       [n for n in nodes if n['title'] == normalize_title(section)])
            if not matches:
                raise ValueError('unknown_section')
            if len(matches) > 1:
                raise ValueError('ambiguous_section')
            node = matches[0]
            if node['start_line'] not in read:
                raise ValueError('section_not_visible')
        body = self.body_lines(post, nodes, node['start_line'] if node else 1, node['end_line'] if node else None)
        if not body:
            raise ValueError('section_has_no_body' if node else 'no_body_evidence')
        selected = [n for n in body if n in read]
        if not selected:
            raise ValueError('section_not_visible' if node else 'no_body_evidence')
        return post, read, node, selected, len(selected) == len(body)

    def entries(self):
        for post_id in sorted({p for p, _ in self.indexed}):
            try:
                post, read, nodes = self.source(post_id)
            except ValueError:
                continue
            # Only visible headings may be suggested; the snapshot is not evidence.
            for node in [None] + [n for n in nodes if n['start_line'] in read]:
                section_id = node['section_id'] if node else None
                try:
                    _, _, _, selected, complete = self.resolve(post_id, section_id)
                except ValueError:
                    continue
                yield {'post_id': post_id, 'version': post['version'], 'title': post['title'],
                       'section': node['title'] if node else None, 'section_id': section_id,
                       'parent_id': node['parent_id'] if node else None,
                       'marker': '[[' + post_id + ('|' + section_id if node else '') + ']]',
                       'visible_ranges': line_ranges(selected), 'complete': complete}
