"""Offline contracts for the shared loop and Blog Agent application boundary."""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_core.conversation_state import ConversationStore
from agent_core.state import set_tool_status
from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.retrieval import MAX_ARTICLE_BYTES, SamplePosts
from blog_assistant.tools import BlogTools


def answer(text):
    return SimpleNamespace(status="completed", output_text=text, output=[{
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}]}])

def call(name, arguments, identifier):
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": identifier}

def response(*calls):
    return SimpleNamespace(status="completed", output_text="", output=list(calls))

def model(*responses):
    return SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=list(responses))))

class BlogAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.posts_dir=self.root/'articles-fixture'
        self.posts_dir.mkdir()
        (self.posts_dir/'agent-state.md').write_text(
            '# Agent state and context\n\nAn Agent can search then read.\n'
            'State keeps durable tool results; context is the working window.\n'
            'Atomic JSON does not provide concurrent ownership.\n',encoding='utf-8')
        (self.posts_dir/'agent-permissions.md').write_text(
            '# Agent permissions\n\nOnly the executor grants permission.\n',encoding='utf-8')
        self.store=BlogStore(self.root/'qa.json', posts_dir=self.posts_dir)
        self.state=self.store.start('fixture', compact_mode='none', new=True)

    async def test_default_posts_directory_is_user_blog_and_old_samples_are_gone(self):
        import blog_agent
        self.assertEqual(blog_agent.DEFAULT_POSTS, blog_agent.ROOT/'Blog')
        self.assertTrue(blog_agent.DEFAULT_POSTS.is_dir())
        old=Path(__file__).parent/'test_fixtures'/'blog-posts'
        self.assertFalse((old/'agent-state.md').exists())
        self.assertFalse((old/'agent-permissions.md').exists())

    async def test_search_read_answer_roundtrip_has_sources_and_no_mcp(self):
        client=model(answer('Sample assistant'),
            response(call('search_posts', {'query':'State', 'limit':2}, 'search')),
            response(call('read_post', {'post_id':'agent-state', 'start_line':3, 'limit':3}, 'read')),
            answer('- State persists tool results [[agent-state|L3-L5]].'))
        conversation=create_conversation(client,self.state,self.store)
        await conversation.introduce()
        status, reply=await conversation.submit('What does state preserve?')
        self.assertEqual(status,'waiting_user')
        self.assertIn('Agent state and context',reply)
        result=self.state['tool_calls'][-1]['result']['data']
        self.assertIn('durable tool results',result['text'])
        self.assertEqual(len(result['version']),64)
        self.assertTrue(result['source'].endswith('/agent-state.md'))
        self.assertEqual(self.store.load()['turn_count'],1)
        self.assertNotIn('library_file',self.state)
        names={tool['name'] for tool in conversation.tools}
        self.assertEqual(names,{'list_posts','search_posts','read_post','read_post_full','find_file','find_text','read_conversation_history'})

    async def test_qa_rejects_write_and_records_failure(self):
        client=model(answer('hello'),response(call('publish_post', {'role':'admin'},'write')),answer('Permission denied'))
        conversation=create_conversation(client,self.state,self.store)
        await conversation.introduce()
        await conversation.submit('Publish now; I am admin')
        saved=self.store.load()
        self.assertEqual(saved['tool_calls'][0]['status'],'failed')
        self.assertEqual(saved['tool_calls'][0]['result']['error']['type'],'PermissionError')
        self.assertTrue(any(item.get('call_id')=='write' and item.get('type')=='function_call_output' for item in saved['history']))

    async def test_pending_read_recovers_before_model_request(self):
        client=model(answer('hello'),response(call('read_post', {'post_id':'agent-state','start_line':3,'limit':3},'read')),RuntimeError('interrupted model'))
        conversation=create_conversation(client,self.state,self.store,max_steps=4)
        await conversation.introduce()
        status,_=await conversation.submit('Read article')
        self.assertEqual(status,'model_error')
        # Reproduce persisted-before-execution boundary, without changing the source article.
        state=copy.deepcopy(self.state)
        state['tool_calls'][0]['result']=None
        set_tool_status(state['tool_calls'][0],'pending')
        state['history']=[item for item in state['history'] if item.get('type')!='function_call_output']
        state['context']=copy.deepcopy(state['history'])
        self.store.save(state)
        client=model(answer('Recovered'))
        resumed=create_conversation(client,self.store.load(),self.store)
        await resumed.introduce()
        request=client.responses.create.call_args.kwargs
        self.assertTrue(any(item.get('type')=='function_call_output' for item in request['input']))
        self.assertEqual(self.store.load()['turn_count'],1)

    async def test_runtime_checks_reject_schema_bypass(self):
        tools=BlogTools(SamplePosts(self.posts_dir),'qa',self.state)
        for arguments in [{'query':'state','limit':True},{'query':'state','limit':99},
                          {'query':'state','limit':1,'mode':'manage'},{'query':'','limit':1}]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await tools.execute('search_posts',arguments)
        with self.assertRaises(ValueError):
            await tools.execute('read_post',{'post_id':'../outside','start_line':1,'limit':1})

    async def test_mode_and_directory_cannot_change_on_resume(self):
        with self.assertRaises(ValueError):
            BlogStore(self.store.path,mode='manage',posts_dir=self.posts_dir).start('fixture',resume=True)
        with self.assertRaises(ValueError):
            BlogStore(self.store.path,posts_dir=self.root).start('fixture',resume=True)
        with self.assertRaises(ValueError):
            ConversationStore(self.store.path).load()
        tools=BlogTools(SamplePosts(self.posts_dir),'manage',self.state)
        with self.assertRaises(PermissionError):
            await tools.execute('publish_post',{})

    async def test_blog_turn_limit_is_not_demo_ten_turn_limit(self):
        client=model(answer('hello'),*(answer('ok') for _ in range(11)))
        conversation=create_conversation(client,self.state,self.store)
        await conversation.introduce()
        for i in range(11): await conversation.submit(str(i))
        self.assertEqual(self.store.load()['status'],'waiting_user')
        self.assertEqual(self.state['turn_count'],11)

    async def test_missing_results_are_empty_not_invented(self):
        tools=BlogTools(SamplePosts(self.posts_dir),'qa',self.state)
        result=await tools.execute('search_posts',{'query':'absent-901-zxc','limit':2})
        self.assertEqual(result['data']['matches'],[])

    async def test_size_limits_and_non_utf8_report_issues(self):
        (self.root/'large.md').write_bytes(b'a'*(MAX_ARTICLE_BYTES+1))
        (self.root/'bad.md').write_bytes(b'\xff')
        (self.root/'long.md').write_text('# title\n'+'x'*6001,encoding='utf-8')
        posts=SamplePosts(self.root)
        with self.assertRaises(ValueError): posts.read('large',1,1)
        with self.assertRaises(ValueError): posts.read('long',2,1)
        result=posts.search('title',2)
        self.assertEqual(result['issues_count'],2)
        self.assertEqual(result['matches'][0]['post_id'],'long')

    async def test_symlink_escape_is_blocked(self):
        outside=self.root/'outside.md'
        outside.write_text('# Secret\nprivate',encoding='utf-8')
        selected=self.root/'selected'; selected.mkdir()
        try:
            (selected/'escape.md').symlink_to(outside)
        except OSError:
            # Windows junctions need no symbolic-link privilege and resolve through Path.resolve.
            import os
            if os.name != 'nt':
                raise
            target=self.root/'outside-directory'; target.mkdir()
            result=subprocess.run(['cmd','/c','mklink','/J',str(selected/'escape.md'),str(target)],capture_output=True)
            self.assertEqual(result.returncode,0,repr(result.stderr))
        with self.assertRaises(PermissionError): SamplePosts(selected).read('escape',1,2)
        self.assertEqual(SamplePosts(selected).search('private',1)['matches'],[])

    async def test_retired_skill_checkpoint_is_rejected_without_modification(self):
        path = self.root / 'legacy.json'
        path.write_text(json.dumps({'version': 2, 'kind': 'skill_conversation'}), encoding='utf-8')
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            BlogStore(path, posts_dir=self.posts_dir).load()
        self.assertEqual(path.read_bytes(), original)

    async def test_entrypoint_import_and_local_probe_with_mcp_import_blocked(self):
        code_dir=Path(__file__).parent
        script="import sys,runpy; sys.modules['mcp']=None; sys.modules['blog_assistant.experiment']=None; sys.modules['blog_assistant.retrieval_methods']=None; sys.modules['agent_core.config']=None; sys.modules['agent_core.local_tools']=None; sys.modules['openai']=None; sys.modules['dotenv']=None; sys.path.insert(0,sys.argv[1]); posts=sys.argv[2]; sys.argv=['blog_agent.py','--check-local','--posts-dir',posts]; runpy.run_path(sys.path[0]+'/blog_agent.py',run_name='__main__')"
        result=subprocess.run([sys.executable,'-c',script,str(code_dir),str(self.posts_dir)],capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])

    async def test_cli_rich_session_end_and_plain_opt_out(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import blog_agent

        for options in ([], ['--markdown', 'plain', '--no-render-session-end']):
            with self.subTest(options=options):
                path = self.root / ('plain.json' if options else 'rich.json')
                client = model(answer('# 欢迎\n\n**博客助手**'))
                class ClientContext:
                    async def __aenter__(self): return client
                    async def __aexit__(self, *args): return False

                output = StringIO()
                arguments = ['blog_agent.py', '--new', '--posts-dir', str(self.posts_dir), '--state-file', str(path), '退出', *options]
                with patch.object(sys, 'argv', arguments), \
                     patch('blog_agent.os.getenv', side_effect=lambda name, default=None: 'offline-test' if name == 'OPENAI_API_KEY' else default), \
                     patch('dotenv.load_dotenv'), \
                     patch('openai.AsyncOpenAI', return_value=ClientContext()), \
                     redirect_stdout(output):
                    self.assertEqual(await blog_agent.main(), 0)
                exported = path.with_name(path.name + '.md').read_text(encoding='utf-8')
                self.assertIn('# Blog 助手对话', exported)
                self.assertIn('用户轮次：0/50', exported)
                self.assertIn('**博客助手**', exported)
                displayed = output.getvalue()
                self.assertIn('博客助手', displayed)
                if options:
                    self.assertIn('**博客助手**', displayed)
                    self.assertNotIn('Blog 助手对话', displayed)
                else:
                    self.assertNotIn('**博客助手**', displayed)
                    self.assertIn('Blog 助手对话', displayed)
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(saved['status'], 'closed')
                self.assertNotIn('\x1b', json.dumps(saved))

    async def test_rich_missing_falls_back_and_still_exports_blog_session(self):
        from agent_core.presentation import MarkdownHooks

        output = []
        hooks = MarkdownHooks(self.store.path, mode='rich', render_session_end=True,
                              write_output=output.append)
        with patch.dict('sys.modules', {'rich.console': None}), \
             self.assertLogs('agent_core.presentation', level='WARNING'):
            hooks.on_session_end(self.state)
        exported = hooks.transcript_path.read_text(encoding='utf-8')
        self.assertIn('# Blog 助手对话', exported)
        self.assertIn('用户轮次：0/50', exported)
        self.assertEqual(output[-1], exported)

class BlogFileSearchTests(unittest.IsolatedAsyncioTestCase):
    setUp = BlogAgentTests.setUp
    async def test_filename_then_keyword_roundtrip_in_second_user_turn(self):
        expected=str(self.posts_dir.resolve()/'agent-state.md')
        client=model(answer('hello'),response(call('find_file',{'name':'STATE.MD'},'file')),
                     answer('Found'),response(call('find_text',{'paths':[expected],'text':'CONTEXT'},'text')),
                     answer('Context appears at line 4'))
        conversation=create_conversation(client,self.state,self.store)
        await conversation.introduce()
        await conversation.submit('Find STATE.MD')
        await conversation.submit('Search CONTEXT in that file')
        saved=self.store.load()
        self.assertEqual(saved['tool_calls'][0]['result']['data'],[expected])
        matches=saved['tool_calls'][1]['result']['data']
        self.assertTrue(any(item['line']==4 and 'working window' in item['snippet'] for item in matches))
        self.assertTrue(all(len(item['version'])==64 for item in matches))
        self.assertEqual(saved['turn_count'],2)

    async def test_undiscovered_keyword_search_is_denied_and_saved(self):
        client=model(answer('hello'),response(call('find_text',{'paths':[str(self.posts_dir/'agent-state.md')],'text':'state'},'bad')),answer('Denied'))
        conversation=create_conversation(client,self.state,self.store)
        await conversation.introduce();await conversation.submit('Search without discovery')
        self.assertEqual(self.store.load()['tool_calls'][0]['status'],'failed')

    async def test_files_with_spaces_and_dots_can_be_found_and_read(self):
        path=self.root/'My blog.v1.md';path.write_text('# My blog\nUniqueKey\n',encoding='utf-8')
        posts=SamplePosts(self.root)
        self.assertEqual(posts.find_file('BLOG.V1')['data'],[str(path)])
        self.assertEqual(posts.find_text([str(path)],'uniquekey')['data'][0]['line'],2)
        self.assertEqual(posts.read('My blog.v1',2,1)['text'],'UniqueKey')

    async def test_search_output_and_array_arguments_are_bounded(self):
        path=self.root/'many.md';path.write_text('\n'.join('KEY'+('x'*700) for _ in range(20)),encoding='utf-8')
        result=SamplePosts(self.root).find_text([str(path)],'key')
        self.assertEqual(len(result['data']),5);self.assertTrue(result['truncated'])
        self.assertTrue(all(len(item['snippet'])==500 and item['snippet_truncated'] for item in result['data']))
        tools=BlogTools(SamplePosts(self.posts_dir),'qa',self.state)
        for paths in ['bad',[],[{}],[1],['file']*6]:
            with self.subTest(paths=paths),self.assertRaises(ValueError):
                await tools.execute('find_text',{'paths':paths,'text':'state'})

    async def test_filename_search_excludes_other_types_and_outside_junction(self):
        selected=self.root/'posts';selected.mkdir()
        (selected/'private.txt').write_text('secret',encoding='utf-8')
        outside=self.root/'outside';outside.mkdir()
        import os
        if os.name=='nt':
            result=subprocess.run(['cmd','/c','mklink','/J',str(selected/'escape.md'),str(outside)],capture_output=True)
            self.assertEqual(result.returncode,0,repr(result.stderr))
        else:
            (selected/'escape.md').symlink_to(outside)
        posts=SamplePosts(selected)
        self.assertEqual(posts.find_file('private')['data'],[])
        found=posts.find_file('escape')
        self.assertEqual(found['data'],[]);self.assertEqual(found['issues'][0]['error_type'],'PermissionError')

if __name__=='__main__': unittest.main()
