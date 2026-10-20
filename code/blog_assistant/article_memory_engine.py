"""Recoverable node generation and fenced atomic publication; never edits articles."""
import hashlib
import json
import time

from .article_memory_build import SummaryRequest, PARSER_VERSION
from .article_memory_plan import plan_article
from .article_memory_runtime import transaction, BudgetBlocked, LeaseLost
from .retrieval import SamplePosts


def read_source(root, article_id, limit=8 * 1024 * 1024):
    posts = SamplePosts(root)
    path = posts._path(article_id)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Article exceeds memory read limit')
    return hashlib.sha256(raw).hexdigest(), raw.decode('utf-8')


def require_target(store, job, token):
    store.require_worker(job['library_id'], token)
    library = store._library(job['library_id'])
    document = store.db.execute('SELECT * FROM documents WHERE library_id=? AND article_id=?',
                                (job['library_id'], job['article_id'])).fetchone()
    if (library['generation_version'] != job['generation_version'] or document['status'] == 'deleted'
            or document['detected_version'] != job['source_version']):
        raise LeaseLost('Build target superseded')
    return library


def build_next(store, library_id, token, generator, *, on_progress=None):
    """Process one article, persisting every completed node before the next call."""
    with transaction(store.db):
        store.require_worker(library_id, token)
        job = store.db.execute("""SELECT * FROM jobs WHERE library_id=? AND node_id='article'
            AND state IN ('pending','retry_wait') AND COALESCE(next_run_at,0)<=?
            ORDER BY job_id LIMIT 1""", (library_id, time.time())).fetchone()
        if not job:
            return False
        job = dict(job)
        library = require_target(store, job, token)
        config = json.loads(library['maintenance_json'])
        if not config or generator is None:
            store.db.execute("UPDATE jobs SET state='blocked_config', reason='model_config_or_key_missing' WHERE job_id=?", (job['job_id'],))
            return True
        store.db.execute("UPDATE jobs SET state='running', attempts=attempts+1, lease_token=? WHERE job_id=?", (token, job['job_id']))
    request_id = None
    try:
        version, body = read_source(library['root'], job['article_id'])
        if version != job['source_version']:
            raise LeaseLost('Source changed before build')
        nodes = plan_article(library_id, job['article_id'], version, body, job['generation_version'], config)
        summaries = {}
        for node in nodes:
            if on_progress is not None:
                on_progress()
            require_target(store, job, token)
            cached = store.db.execute('SELECT summary FROM summary_cache WHERE library_id=? AND cache_key=?',
                                      (library_id, node.cache_key)).fetchone()
            if cached:
                summary = cached[0]
            elif not node.text and not node.dependencies:
                summary = ''  # Empty source is empty, never a fabricated summary.
            else:
                dependencies = tuple(summaries[d] for d in node.dependencies)
                text = json.dumps({'title': node.title, 'context': node.context, 'source': node.text, 'summaries': dependencies}, ensure_ascii=False)
                if len(text) > config['chunk_chars']:
                    raise ValueError('Serialized node input exceeds bound')
                # UTF-8 bytes conservatively bound text tokens; fixed envelope covers prompt.
                request_id = store.reserve(library_id, token, len(text.encode('utf-8')) + 2048,
                                           config['request_output_tokens'])
                request = SummaryRequest(library_id, job['article_id'], version, node.node_id,
                                         job['generation_version'], request_id, text, ())
                result = generator.generate(request)
                if result.request_id != request_id:
                    raise ValueError('Generator returned a different request id')
                if not store.settle(library_id, result):
                    raise ValueError('Unverified usage or reservation exceeded')
                request_id = None
                summary = result.text
                if not isinstance(summary, str) or not summary.strip() or len(summary) > config['summary_chars']:
                    raise ValueError('Invalid or oversized summary')
            with transaction(store.db):
                require_target(store, job, token)
                store.db.execute('INSERT OR IGNORE INTO summary_cache VALUES (?, ?, ?)', (library_id, node.cache_key, summary))
                store.db.execute("""INSERT INTO nodes
                    (library_id,article_id,source_version,generation_version,node_id,parent_id,kind,title,
                     start_line,end_line,citation_id,parser_version,summary,state,dependencies_json,cache_key,coverage_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?,?)
                    ON CONFLICT(library_id,article_id,source_version,generation_version,node_id) DO UPDATE SET
                    summary=excluded.summary,state='ready',dependencies_json=excluded.dependencies_json,
                    cache_key=excluded.cache_key,coverage_json=excluded.coverage_json""",
                    (library_id,job['article_id'],version,job['generation_version'],node.node_id,node.parent_id,
                     node.kind,node.title,node.start_line,node.end_line,node.node_id if node.kind=='section' else None,
                     PARSER_VERSION,summary,json.dumps(node.dependencies),node.cache_key,
                     json.dumps([[node.start_line,node.end_line]])))
            summaries[node.node_id] = summary
        # Source IO stays outside the short write transaction. Readers recheck too.
        if read_source(library['root'], job['article_id'])[0] != version:
            raise LeaseLost('Source changed during build')
        with transaction(store.db):
            require_target(store, job, token)
            ready = store.db.execute("SELECT node_id FROM nodes WHERE library_id=? AND article_id=? AND source_version=? AND generation_version=? AND state='ready'",
                                     (library_id,job['article_id'],version,job['generation_version'])).fetchall()
            if not {n.node_id for n in nodes}.issubset({r[0] for r in ready}):
                raise ValueError('Missing build dependencies')
            store.db.execute("UPDATE documents SET active_version=?,status='ready' WHERE library_id=? AND article_id=?",
                             (version,library_id,job['article_id']))
            store.db.execute("UPDATE jobs SET state='ready',reason=NULL WHERE job_id=?", (job['job_id'],))
    except Exception as exc:
        if request_id:
            store.mark_unknown(library_id, request_id)
        with transaction(store.db):
            # A replaced worker may account its request, but cannot mutate job state.
            try:
                store.require_worker(library_id, token)
            except LeaseLost:
                return True
            state = ('blocked_budget' if isinstance(exc, BudgetBlocked) else
                     'superseded' if isinstance(exc, LeaseLost) else
                     'failed' if isinstance(exc, (ValueError, UnicodeError)) or job['attempts'] + 1 >= 3 else 'retry_wait')
            store.db.execute('UPDATE jobs SET state=?,reason=?,next_run_at=? WHERE job_id=? AND lease_token=?',
                             (state,type(exc).__name__,time.time()+min(60,2**(job['attempts']+1)),job['job_id'],token))
    return True
