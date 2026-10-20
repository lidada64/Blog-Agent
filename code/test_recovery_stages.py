"""Real read/loop/checkpoint boundaries with disposable articles and fake models."""
import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.retrieval import SamplePosts
from test_blog_agent import answer, call, model, response


class RecoveryStageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.posts = self.root / 'posts'
        self.posts.mkdir()
        (self.posts / 'long.md').write_text(
            '# Long\n' + 'Background information.\n' * 300 + '## Target\nTarget fact.\n', encoding='utf-8')
        self.store = BlogStore(self.root / 'qa.json', posts_dir=self.posts)
        self.state = self.store.start('fixture', new=True)

    async def test_empty_discovery_then_full_fallback_still_allows_segment(self):
        client = model(answer('Hello'), response(call('find_file', {'name': 'absent'}, 'empty')),
                       response(call('read_post_full', {'post_id': 'long'}, 'full')),
                       response(call('read_post', {'post_id': 'long', 'start_line': 302, 'limit': 2}, 'segment')),
                       answer('Target fact. [[long|Target]]'))
        conv = create_conversation(client, self.state, self.store)
        await conv.introduce()
        await conv.submit('Explain long')
        self.assertIn('read_post', [t['name'] for t in client.responses.create.call_args_list[3].kwargs['tools']])
        self.assertEqual(self.state['turns'][-1]['result'], 'answered_unverified')
        self.assertEqual(self.state['tool_calls'][-1]['status'], 'completed')

    def test_long_navigation_has_real_ranges_and_skips_code_headings(self):
        (self.posts / 'code.md').write_text('```python\n# fake title\n```\n## Real\n' + 'body\n' * 1500, encoding='utf-8')
        result = SamplePosts(self.posts).read_full('code')
        self.assertEqual(result['title'], 'Real')
        self.assertFalse(result['complete'])
        self.assertEqual(result['text'], '')
        navigation = result['navigation']
        self.assertEqual(navigation['kind'], 'navigation_only')
        self.assertEqual(navigation['items'][0]['title'], 'Real')
        self.assertEqual(navigation['items'][0]['start_line'], 4)
        self.assertLessEqual(len(json.dumps(navigation, ensure_ascii=False).encode()), 4000)

    async def test_navigation_reaches_model_without_becoming_read_evidence(self):
        client = model(answer('Hello'), response(call('read_post_full', {'post_id': 'long'}, 'full')),
                       answer('Target fact. [[long|Target]]'))
        conv = create_conversation(client, self.state, self.store)
        await conv.introduce()
        await conv.submit('Explain long')
        prompt = json.dumps(client.responses.create.call_args.kwargs['input'])
        self.assertIn('navigation_only', prompt)
        self.assertEqual(self.state['turns'][-1]['result'], 'partial')
        self.assertEqual(self.state['turns'][-1]['projection']['visible'], [])

    async def test_protocol_text_is_not_repaired_as_citation(self):
        client = model(answer('Hello'), response(call('read_post', {'post_id': 'long', 'start_line': 302, 'limit': 2}, 'read')),
                       answer('<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="read_post">'))
        conv = create_conversation(client, self.state, self.store)
        await conv.introduce()
        _, reply = await conv.submit('Explain long')
        self.assertEqual(self.state['turns'][-1]['result'], 'protocol_error')
        self.assertNotIn('DSML', reply)
        self.assertNotIn('DSML', json.dumps(self.state['history']))
        self.assertEqual(self.state['turns'][-1]['retrieval']['citation_retries'], 0)
        self.assertEqual(client.responses.create.call_count, 3)

    async def test_checkpoint_denial_during_tool_is_storage_error_and_resumable(self):
        client = model(answer('Hello'), response(call('read_post', {'post_id': 'long', 'start_line': 302, 'limit': 2}, 'read')))
        conv = create_conversation(client, self.state, self.store)
        await conv.introduce()
        original_read = conv.turn_policy.tools.posts.read
        import os
        original_replace = os.replace
        saving_after_read = False

        def read(*args):
            nonlocal saving_after_read
            result = original_read(*args)
            saving_after_read = True
            return result

        def replace(source, target):
            if saving_after_read and Path(target) == self.store.path:
                raise PermissionError(13, 'simulated checkpoint lock')
            return original_replace(source, target)

        with patch.object(conv.turn_policy.tools.posts, 'read', side_effect=read), patch('agent_core.state.os.replace', side_effect=replace):
            status, reply = await conv.submit('Explain long')
        self.assertEqual(status, 'storage_error')
        self.assertIn('保存', reply)
        self.assertFalse(conv.turn_policy.denied_calls())
        self.assertFalse(any(e['event_type'] == 'guard.deny' for e in self.state['trace']['events']))
        restored = self.store.load()
        spent = restored['turns'][-1]['retrieval']['model_requests']
        self.assertEqual(restored['turns'][-1]['status'], 'running')
        resumed = create_conversation(model(answer('Target fact. [[long|Target]]')), restored, self.store)
        await resumed.resume_turn()
        self.assertEqual(restored['turns'][-1]['result'], 'answered_unverified')
        self.assertEqual(restored['turns'][-1]['retrieval']['model_requests'], spent + 1)
        self.assertEqual(restored['tool_calls'][0]['status'], 'completed')

    async def test_repeated_navigation_after_resume_does_not_reset_progress_or_budget(self):
        client = model(answer('Hello'), response(call('read_post_full', {'post_id': 'long'}, 'full')),
                       asyncio.CancelledError())
        conv = create_conversation(client, self.state, self.store, max_steps=10)
        await conv.introduce()
        with self.assertRaises(asyncio.CancelledError):
            await conv.submit('Explain long')
        restored = self.store.load()
        spent = restored['turns'][-1]['retrieval']['model_requests']
        client = model(response(call('read_post_full', {'post_id': 'long'}, 'again')),
                       response(call('read_post_full', {'post_id': 'long'}, 'again2')), answer('Not enough information.'))
        resumed = create_conversation(client, restored, self.store, max_steps=10)
        await resumed.resume_turn()
        budget = restored['turns'][-1]['retrieval']
        self.assertEqual(budget['no_progress'], 2)
        self.assertEqual(budget['reason'], 'no_progress')
        self.assertEqual(budget['phase'], 'answering')
        self.assertEqual(budget['model_requests'], spent + 3)
        self.assertEqual(client.responses.create.call_args.kwargs['tools'], [])

    def test_unheaded_and_large_navigation_are_bounded(self):
        for body in ['text\n' * 2000, ''.join(f'## 标题{i}\n正文\n' for i in range(1000))]:
            (self.posts / 'nav.md').write_text(body, encoding='utf-8')
            data = SamplePosts(self.posts).read_full('nav')
            nav = data['navigation']
            self.assertTrue(nav['truncated'])
            self.assertIsInstance(nav['next_start_line'], int)
            self.assertLessEqual(len(nav['items']), 20)
            self.assertLessEqual(len(json.dumps(nav, ensure_ascii=False).encode()), 4000)

    async def test_stopped_reading_is_not_overwritten_by_citation_failure(self):
        client = model(answer('Hello'),
                       response(call('read_post', {'post_id': 'long', 'start_line': 302, 'limit': 2}, 'read')),
                       response(call('read_post', {'post_id': 'long', 'start_line': 302, 'limit': 2}, 'again')),
                       response(call('read_post', {'post_id': 'long', 'start_line': 302, 'limit': 2}, 'again2')),
                       answer('Cannot finish.'))
        conv = create_conversation(client, self.state, self.store)
        await conv.introduce()
        _, reply = await conv.submit('Explain long')
        turn = self.state['turns'][-1]
        self.assertEqual(turn['result'], 'partial')
        self.assertEqual(turn['failure']['cause'], 'no_progress')
        self.assertEqual(turn['retrieval']['citation_retries'], 0)
        self.assertIn('no_progress', reply)

    async def test_filesystem_permission_error_is_not_policy_denial(self):
        client = model(answer('Hello'), response(call('read_post', {'post_id': 'long', 'start_line': 1, 'limit': 2}, 'read')),
                       answer('Cannot read.'))
        conv = create_conversation(client, self.state, self.store)
        await conv.introduce()
        with patch.object(conv.turn_policy.tools.posts, 'read', side_effect=PermissionError('source locked')):
            _, reply = await conv.submit('Explain long')
        error = self.state['tool_calls'][0]['result']['error']
        self.assertEqual(error['category'], 'io')
        self.assertEqual(self.state['turns'][-1]['result'], 'tool_error')
        self.assertIn('文件访问错误', reply)
        self.assertFalse(conv.turn_policy.denied_calls())
        self.assertNotIn('越权', reply)

    def test_ordinary_protocol_explanation_is_not_rejected(self):
        from blog_assistant.convergence import RetrievalPolicy
        self.assertFalse(RetrievalPolicy.protocol_text('DSML is a protocol; for example <｜｜DSML｜｜ calls>.'))
