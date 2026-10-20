"""Exercise the real conversation loop with derived summaries and persisted selection."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from blog_assistant.article_memory_engine import build_next
from blog_assistant.article_memory_read import ArticleMemoryReader
from blog_assistant.article_memory_store import ArticleMemoryStore
from blog_assistant.article_memory_sync import synchronize
from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.tools import BlogTools
from blog_assistant.retrieval import SamplePosts
from test_article_memory_runtime import config, FakeGenerator


def response(name, arguments, identifier):
    return SimpleNamespace(status='completed',output_text='',output=[
        {'type':'function_call','name':name,'arguments':json.dumps(arguments),'call_id':identifier}])


class MemoryConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'posts'
        self.root.mkdir()
        self.article = self.root/'a.md'
        self.article.write_text('# Root\n\nIntro.\n\n## Detail\n\nA precise fact.\n',encoding='utf-8')
        self.db = Path(self.temp.name)/'memory.sqlite3'
        with ArticleMemoryStore(self.db) as store:
            library = store.register_library(self.root,'qa:local-v1')
            store.configure(library,config())
            synchronize(store,library)
            token = store.claim_worker(library,'test')
            store.heartbeat(library,token,os.getpid())
            build_next(store,library,token,FakeGenerator())
            store.release_worker(library,token,'test_complete')
        self.reader = ArticleMemoryReader(self.db,self.root,'qa:local-v1')
        self.store = BlogStore(Path(self.temp.name)/'qa.json',posts_dir=self.root)
        self.state = self.store.start('fixture',new=True)

    async def asyncSetUp(self):
        intro = SimpleNamespace(status='completed',output_text='Hello',output=[
            {'type':'message','role':'assistant','status':'completed',
             'content':[{'type':'output_text','text':'Hello','annotations':[]}]}])
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=intro)))
        await create_conversation(client,self.state,self.store,memory=self.reader).introduce()

    async def test_overview_then_restart_and_number_selection(self):
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response(
            'get_article_overview',{'post_id':'a','offset':0},'overview'))))
        conversation = create_conversation(client,self.state,self.store,memory=self.reader)
        _, reply = await conversation.submit('总结文章a并展示目录')
        self.assertIn('FIXTURE',reply)
        self.assertIn('2. Detail',reply)
        self.assertEqual(client.responses.create.await_count,1)
        self.assertEqual(self.state['turns'][-1]['derived_evidence']['evidence_type'],'derived_summary')
        saved = self.store.load()
        self.assertEqual(saved['article_memory_selection']['numbers']['2'][:3],'@s-')
        next_client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response(
            'read_article_section',{'post_id':'a','selector':'2'},'chapter'))))
        conversation = create_conversation(next_client,saved,self.store,memory=self.reader)
        _, reply = await conversation.submit('总结第2章')
        self.assertIn('Detail',reply)
        self.assertIn('派生摘要',reply)
        request = next_client.responses.create.await_args.kwargs
        self.assertTrue(any('当前已展示章节绑定' in str(item) for item in request['input']))

    async def test_cold_start_returns_truthful_directory_without_generator(self):
        self.article.write_text('# Changed\n\nNew body\n',encoding='utf-8')
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response(
            'get_article_overview',{'post_id':'a','offset':0},'cold'))))
        _, reply = await create_conversation(client,self.state,self.store,memory=self.reader).submit('总结文章a')
        self.assertIn('尚未就绪',reply)
        self.assertIn('Changed',reply)
        self.assertNotIn('FIXTURE',reply)
        self.assertEqual(client.responses.create.await_count,1)

    async def test_scope_and_argument_validation(self):
        with self.assertRaises(PermissionError):
            BlogTools(SamplePosts(self.root),'manage',self.state,memory=self.reader)
        tools = BlogTools(SamplePosts(self.root),'qa',self.state,memory=self.reader)
        with self.assertRaises(ValueError):
            await tools.execute('get_article_overview',{'post_id':'a','offset':-1})
        with self.assertRaises(ValueError):
            await tools.execute('get_article_overview',{'post_id':'../a','offset':0})
        with self.assertRaises(ValueError):
            await tools.execute('read_article_section',{'post_id':'a','selector':'2'})

    async def test_old_prompt_does_not_silently_gain_memory_tools(self):
        self.state['instructions'] = 'Old saved instructions'
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock()))
        conversation = create_conversation(client,self.state,self.store,memory=self.reader)
        self.assertNotIn('get_article_overview',{tool['name'] for tool in conversation.tools})

    async def test_multiple_chapters_all_delivered(self):
        first = response('read_article_section',{'post_id':'a','selector':'Root'},'one')
        second = response('read_article_section',{'post_id':'a','selector':'Detail'},'two')
        first.output.extend(second.output)
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=first)))
        _,reply = await create_conversation(client,self.state,self.store,memory=self.reader).submit('分别总结Root和Detail')
        self.assertIn('Root',reply)
        self.assertIn('Detail',reply)
        self.assertEqual(len(self.state['turns'][-1]['derived_evidence']),2)

    async def test_original_details_remain_body_evidence(self):
        tools = BlogTools(SamplePosts(self.root),'qa',self.state,memory=self.reader)
        data = (await tools.execute('read_post',{'post_id':'a','start_line':7,'limit':1}))['data']
        self.assertEqual(data['text'],'A precise fact.')
        self.assertNotIn('derived_summary',data)


if __name__ == '__main__':
    unittest.main()
