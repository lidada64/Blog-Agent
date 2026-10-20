"""Memory B–D acceptance with synthetic articles, real SQLite/processes and fake models."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from blog_assistant.article_memory_build import SummaryResult
from blog_assistant.article_memory_engine import build_next
from blog_assistant.article_memory_plan import plan_article
from blog_assistant.article_memory_read import ArticleMemoryReader
from blog_assistant.article_memory_runtime import BudgetBlocked, LeaseLost
from blog_assistant.article_memory_store import ArticleMemoryStore, _SCHEMA
from blog_assistant.article_memory_sync import synchronize
from blog_assistant.article_memory_worker import ensure_worker


def config(**overrides):
    return dict(model='synthetic-test',base_url='http://127.0.0.1:1/v1',prompt_version='v1',currency='test',
                max_requests=1000,max_input_tokens=1_000_000,max_output_tokens=1_000_000,
                max_cost_microunits=1_000_000,input_price_per_million=1_000_000,
                output_price_per_million=1_000_000,chunk_chars=2048,summary_chars=256,
                request_output_tokens=256) | overrides


class FakeGenerator:
    def __init__(self, hook=None):
        self.calls, self.hook = [], hook

    def generate(self, request):
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        data = json.loads(request.text)
        # Fixture-only marker. Never registered as a production provider.
        summary = ('FIXTURE ' + data['title'] + ' ' + data['source'])[:220]
        return SummaryResult(summary, request.request_id, 20, 30, 50)


class MemoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'posts'
        self.root.mkdir()
        self.db_path = Path(self.tmp.name)/'memory.sqlite3'
        self.store = ArticleMemoryStore(self.db_path)
        self.addCleanup(self.store.close)
        self.library = self.store.register_library(self.root,'qa:local-v1')
        self.article = self.root/'a.md'
        self.article.write_text('# Main\n\nIntro.\n\n## First\n\nOne.\n\n## Second\n\nTwo.\n',encoding='utf-8')

    def prepare(self, **overrides):
        self.store.configure(self.library,config(**overrides))
        synchronize(self.store,self.library)
        token = self.store.claim_worker(self.library,'test')
        self.store.heartbeat(self.library,token,os.getpid())
        return token

    def reader(self):
        return ArticleMemoryReader(self.db_path,self.root,'qa:local-v1')

    def test_v1_migration_preserves_inventory(self):
        old_path = Path(self.tmp.name)/'old.sqlite3'
        with closing(sqlite3.connect(old_path)) as old:
            for statement in _SCHEMA:
                old.execute(statement)
            old.execute("INSERT INTO libraries(library_id,root,scope,generation_version) VALUES ('old','root','scope','v1')")
            old.execute('PRAGMA user_version=1')
            old.commit()
        with ArticleMemoryStore(old_path) as migrated:
            self.assertEqual(migrated._library('old')['generation_version'],'v1')
            self.assertEqual(migrated.db.execute('PRAGMA user_version').fetchone()[0],2)

    def test_concurrent_claims_and_expired_lease_recovery(self):
        def claim(owner):
            with ArticleMemoryStore(self.db_path) as store:
                return store.claim_worker(self.library,owner,ttl=30)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(claim,['one','two']))
        self.assertEqual(sum(t is not None for t in tokens),1)
        old = next(t for t in tokens if t is not None)
        with patch('blog_assistant.article_memory_runtime.time.time',return_value=time.time()+31):
            newer = self.store.claim_worker(self.library,'after-crash')
        self.assertGreater(newer,old)
        with self.assertRaises(LeaseLost):
            self.store.require_worker(self.library,old)

    def test_usage_rollup_keeps_unknown_and_deduplicates_same_checkpoint(self):
        from blog_assistant.article_memory_usage import project_usage
        token = self.prepare()
        self.store.reserve(self.library,token,10,20)
        path = Path(self.tmp.name)/'foreground.json'
        path.write_text(json.dumps({'model_requests':[{'model':'fixture','input_tokens':7,'output_tokens':None}]}),encoding='utf-8')
        report = project_usage(self.store,[path,path])
        self.assertEqual(report['requests'],2)
        self.assertEqual(len(report['foreground']),1)
        self.assertIsNone(report['total_cost'])
        self.assertFalse(report['cost_complete'])
        self.assertEqual(report['foreground'][0]['missing_usage_requests'],1)

    def test_context_movement_changes_cache_keys(self):
        original = '# Main\n\n## Parent A\n\n### Child\n\nSame body.\n'
        moved = original.replace('Parent A','Parent B')
        a = plan_article('scope','a','v1',original,'gen',config())
        b = plan_article('scope','a','v2',moved,'gen',config())
        self.assertNotEqual(next(n.cache_key for n in a if n.title=='Child'),next(n.cache_key for n in b if n.title=='Child'))
        self.assertIn('Parent B',next(n.context for n in b if n.title=='Child'))

    def test_complete_build_and_incremental_chapter_reuse(self):
        token = self.prepare()
        first = FakeGenerator()
        self.assertTrue(build_next(self.store,self.library,token,first))
        overview = self.reader().read('a')
        self.assertEqual(overview['state'],'ready')
        self.assertEqual(overview['evidence_type'],'derived_summary')
        self.assertFalse(overview['whole_body_read'])
        self.article.write_text(self.article.read_text(encoding='utf-8').replace('One.','Changed.'),encoding='utf-8')
        synchronize(self.store,self.library,token=token)
        second = FakeGenerator()
        build_next(self.store,self.library,token,second)
        self.assertLess(len(second.calls),len(first.calls))
        self.assertFalse(any('Two.' in call.text or json.loads(call.text)['title']=='Second' for call in second.calls))
        self.assertEqual(self.reader().read('a')['state'],'ready')

    def test_source_changed_during_build_never_publishes(self):
        token = self.prepare()
        generator = FakeGenerator(lambda _: self.article.write_text('# Changed\n\nnew.',encoding='utf-8'))
        build_next(self.store,self.library,token,generator)
        self.assertNotEqual(self.reader().read('a')['state'],'ready')
        self.assertIsNone(self.store.db.execute('SELECT active_version FROM documents').fetchone()[0])
        self.assertEqual(self.store.status(self.library)['jobs'],{'superseded':1})

    def test_old_worker_cannot_commit_after_pause_or_takeover(self):
        token = self.prepare()
        build_next(self.store,self.library,token,FakeGenerator(lambda _: self.store.pause(self.library)))
        self.assertIsNone(self.store.db.execute('SELECT active_version FROM documents').fetchone()[0])
        with self.assertRaises(LeaseLost):
            self.store.heartbeat(self.library,token,os.getpid())
        self.assertIsNone(self.store.claim_worker(self.library,'another'))
        self.store.resume_worker(self.library)
        newer = self.store.claim_worker(self.library,'another')
        self.assertGreater(newer,token)

    def test_unknown_consumption_and_restart_do_not_refill(self):
        token = self.prepare(max_requests=1)
        with self.assertRaises(ValueError):
            self.store.reserve(self.library,token,-1,100)
        request = self.store.reserve(self.library,token,100,200)
        before = self.store.budget_status(self.library)
        self.store.release_worker(self.library,token,'crash')
        with ArticleMemoryStore(self.db_path) as recovered:
            new = recovered.claim_worker(self.library,'recovery')
            self.assertEqual(recovered.budget_status(self.library),before)
            self.assertEqual(recovered.db.execute('SELECT state FROM usage WHERE request_id=?',(request,)).fetchone()[0],'unknown')
            with self.assertRaises(BudgetBlocked):
                recovered.reserve(self.library,new,1,1)

    def test_budget_blocks_before_call_and_explicit_raise_resumes(self):
        token = self.prepare(max_requests=1)
        generator = FakeGenerator()
        build_next(self.store,self.library,token,generator)
        self.assertEqual(len(generator.calls),1)
        self.assertEqual(self.store.status(self.library)['jobs'],{'blocked_budget':1})
        old_used = self.store.budget_status(self.library)['used']['requests']
        self.store.configure(self.library,config(max_requests=100))
        token = self.store.claim_worker(self.library,'resumed')
        build_next(self.store,self.library,token,generator)
        self.assertEqual(self.reader().read('a')['state'],'ready')
        self.assertGreater(self.store.budget_status(self.library)['used']['requests'],old_used)

    def test_timeout_is_unknown_and_has_finite_retry(self):
        token = self.prepare()
        def fail(_):
            raise TimeoutError('fixture')
        for _ in range(3):
            build_next(self.store,self.library,token,FakeGenerator(fail))
            self.store.db.execute('UPDATE jobs SET next_run_at=0')
        self.assertEqual(self.store.status(self.library)['jobs'],{'failed':1})
        self.assertEqual(self.store.budget_status(self.library)['reserved_or_unknown']['requests'],3)

    def test_oversized_code_is_failed_without_calls(self):
        self.article.write_text('# Code\n\n```python\n'+'x'*3000+'\n```\n',encoding='utf-8')
        token = self.prepare()
        generator = FakeGenerator()
        build_next(self.store,self.library,token,generator)
        self.assertEqual(generator.calls,[])
        self.assertEqual(self.store.status(self.library)['jobs'],{'failed':1})

    def test_many_sections_use_bounded_recursive_merges(self):
        self.article.write_text('# Main\n\n'+''.join(f'## S{i}\n\nFact {i}.\n\n' for i in range(40)),encoding='utf-8')
        token = self.prepare()
        generator = FakeGenerator()
        build_next(self.store,self.library,token,generator)
        self.assertEqual(self.reader().read('a')['state'],'ready')
        self.assertTrue(any(':merge:' in r.node_id for r in generator.calls))
        self.assertTrue(all(len(r.text)<=2048 for r in generator.calls))
        self.assertIsNotNone(self.reader().read('a')['next_offset'])

    def test_selection_persists_and_rejects_changed_version(self):
        token = self.prepare()
        build_next(self.store,self.library,token,FakeGenerator())
        reader,state = self.reader(),{}
        overview = reader.read('a')
        reader.bind(overview,state)
        restored = json.loads(json.dumps(state))
        chapter = reader.read('a',selector='2',selection=restored['article_memory_selection'])
        self.assertEqual(chapter['title'],'First')
        self.assertEqual(reader.read('a',selector='Second')['title'],'Second')
        self.article.write_text('# Different\n',encoding='utf-8')
        with self.assertRaises(ValueError):
            reader.read('a',selector='2',selection=restored['article_memory_selection'])
        self.assertIsNone(reader.read('a')['summary'])

    def test_delete_scope_and_late_delivery_revalidation(self):
        token = self.prepare()
        build_next(self.store,self.library,token,FakeGenerator())
        reader = self.reader()
        result = reader.read('a')
        isolated = self.store.register_library(self.root,'manage:local-v1')
        self.assertNotEqual(isolated,self.library)
        other = ArticleMemoryReader(self.db_path,self.root,'manage:local-v1')
        self.assertIsNone(other.read('a')['summary'])
        self.article.unlink()
        with self.assertRaises(OSError):
            reader.refresh(result,None)

    def test_provider_exceeds_reservation_pauses_and_accounts_actual(self):
        token = self.prepare()
        request = self.store.reserve(self.library,token,10,10)
        self.assertFalse(self.store.settle(self.library,SummaryResult('x',request,20,20,40)))
        self.assertTrue(self.store.worker(self.library)['paused'])
        self.assertEqual(self.store.budget_status(self.library)['used']['input_tokens'],20)

    def test_config_change_invalidates_active_summary_without_budget_reset(self):
        token = self.prepare()
        build_next(self.store,self.library,token,FakeGenerator())
        before = self.store.budget_status(self.library)['used']
        self.store.configure(self.library,config(prompt_version='v2'))
        self.assertIsNone(self.reader().read('a')['summary'])
        self.assertEqual(self.store.budget_status(self.library)['used'],before)
        with self.assertRaises(LeaseLost):
            self.store.require_worker(self.library,token)

    def test_independent_worker_scan_pause_and_parent_exit(self):
        # Launch from a short-lived parent: the child must outlive it.
        script = ("from blog_assistant.article_memory_worker import ensure_worker; "
                  f"print(ensure_worker({str(self.db_path)!r},{self.library!r},interval=0.15)['state'])")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent))
        env.pop('BLOG_MEMORY_API_KEY',None)
        result = subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('running',result.stdout)
        self.addCleanup(self.stop_worker)
        second = ensure_worker(self.db_path,self.library,interval=0.15)
        first_token = second['token']
        self.assertEqual(second['state'],'running')
        (self.root/'b.md').write_text('# New\n',encoding='utf-8')
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            if self.store.db.execute("SELECT 1 FROM documents WHERE article_id='b'").fetchone():
                break
            time.sleep(0.1)
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM documents WHERE article_id='b'").fetchone())
        self.store.pause(self.library)
        self.assertEqual(ensure_worker(self.db_path,self.library)['state'],'paused')
        self.assertGreater(self.store.worker(self.library)['token'],first_token)

    def stop_worker(self):
        self.store.pause(self.library)
        time.sleep(1.3)  # Child heartbeat/scan exits and releases Windows file handles.


if __name__ == '__main__':
    unittest.main()
