"""Version/visibility-checked derived evidence, kept separate from read body evidence."""
import json
from pathlib import Path

from .article_memory_build import index_nodes
from .article_memory_engine import read_source
from .article_memory_store import ArticleMemoryStore

MEMORY_TOOLS = frozenset({'get_article_overview', 'read_article_section'})


class ArticleMemoryReader:
    def __init__(self, database, root, scope):
        self.database, self.root, self.scope = Path(database), str(Path(root).resolve()), scope

    def current(self, article_id):
        version, body = read_source(self.root, article_id)
        return version, index_nodes(article_id, version, body)

    def read(self, article_id, *, selector=None, offset=0, selection=None):
        version, nodes = self.current(article_id)  # Current path/permission check even if cached DB is ready.
        sections = nodes[1:] or [nodes[0]]
        selected = nodes[0]
        if selector is not None:
            if selector.isdecimal():
                if (not selection or selection.get('post_id') != article_id or selection.get('version') != version
                        or selection.get('scope') != self.scope or selection.get('root') != self.root):
                    raise ValueError('Section numbers require a current displayed directory for this article')
                node_id = selection['numbers'].get(selector)
                matches = [n for n in sections if n['node_id'] == node_id]
            else:
                matches = [n for n in sections if n['node_id'] == selector or n['title'] == selector]
            if len(matches) != 1:
                raise ValueError('Unknown or ambiguous chapter; display its directory first')
            selected = matches[0]
        if offset < 0 or (offset >= len(sections) and offset != 0):
            raise ValueError('Directory offset out of range')
        page = sections[offset:offset + 8] if selector is None else []
        summary, state, generation = None, 'not_initialized', None
        descriptions = {}
        if self.database.exists():
            with ArticleMemoryStore(self.database) as store:
                library_id = store.find_library(self.root, self.scope)
                library = store._library(library_id)
                generation = library['generation_version']
                document = store.db.execute('SELECT * FROM documents WHERE library_id=? AND article_id=?', (library_id,article_id)).fetchone()
                state = 'not_indexed'
                if document:
                    state = document['status']
                    if document['active_version'] == version and document['status'] == 'ready':
                        records = store.db.execute("SELECT * FROM nodes WHERE library_id=? AND article_id=? AND source_version=? AND generation_version=? AND state='ready'",
                                                   (library_id,article_id,version,generation)).fetchall()
                        descriptions = {r['node_id']: r['summary'] for r in records}
                        summary = descriptions.get(selected['node_id'])
                        state = 'ready' if summary is not None else 'stale'
                    else:
                        state = 'stale' if document['detected_version'] != version else state
                    if state != 'ready':
                        job = store.db.execute("SELECT state FROM jobs WHERE library_id=? AND article_id=? AND source_version=? AND generation_version=? AND node_id='article'",
                                               (library_id,article_id,version,generation)).fetchone()
                        state = job[0] if job else state
        # Catch edits between filesystem read and DB lookup; do not deliver old text.
        if read_source(self.root, article_id)[0] != version:
            raise ValueError('Source changed during summary read')
        return {'evidence_type': 'derived_summary' if summary is not None else 'directory_metadata',
                'post_id': article_id, 'version': version, 'generation_version': generation,
                'node_id': selected['node_id'], 'title': selected['title'], 'summary': summary,
                'state': state, 'source_range': [selected['start_line'],selected['end_line']],
                'whole_body_read': False, 'offset': offset, 'selector': selector,
                'chapters': [{'number': offset+i+1, 'node_id': n['node_id'], 'title': n['title'][:200],
                              'description': descriptions.get(n['node_id'], '')[:120],
                              'range': [n['start_line'],n['end_line']]} for i,n in enumerate(page)],
                'next_offset': offset+len(page) if selector is None and offset+len(page)<len(sections) else None}

    def refresh(self, result, selection):
        current = self.read(result['post_id'], selector=result['selector'], offset=result['offset'], selection=selection)
        if current['version'] != result['version'] or current['generation_version'] != result['generation_version']:
            raise ValueError('Derived evidence changed before delivery; read again')
        return current

    def bind(self, result, state):
        if result['selector'] is None:
            state['article_memory_selection'] = {'post_id':result['post_id'], 'version':result['version'],
                'scope':self.scope, 'root':self.root,
                'numbers':{str(n['number']):n['node_id'] for n in result['chapters']}}


def render_memory(result, english=False):
    parts = [result['summary']] if result['summary'] else [
        ('The overview is not available yet. You can choose a chapter or read its source.' if english else
         '文章摘要尚未就绪，可以先选择章节或阅读原文。') + f" ({result['state']})"]
    if result['chapters']:
        parts.append('\n'.join(f"{n['number']}. {n['title']}" + (f"：{n['description']}" if n['description'] else '') for n in result['chapters']))
        if result['next_offset'] is not None:
            parts.append(('More chapters: next offset ' if english else '还有后续目录，下一页 offset：') + str(result['next_offset']))
        parts.append('Which chapter would you like to explore?' if english else '你想深入哪一章？可以回复编号或章节标题。')
    parts.append(('Source (derived summary): ' if english else '来源（派生摘要）：') + result['post_id'] + ' / ' + result['title']
                 if result['summary'] else ('Source directory: ' if english else '目录来源：') + result['post_id'])
    return '\n\n'.join(parts)


def selection_context(state):
    selection = state.get('article_memory_selection')
    return ('\n当前已展示章节绑定（编号只适用于该文章和版本；切题不套用）：' + json.dumps(selection,ensure_ascii=False)
            if selection else '')
