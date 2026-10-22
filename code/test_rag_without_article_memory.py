"""Article answers must use source bodies after retiring summary storage."""
import unittest
import test_blog_agent as fixtures
from test_blog_agent import model, answer, response, call
from blog_assistant.session import create_conversation
from blog_assistant.tools import BlogTools
from blog_assistant.retrieval import SamplePosts
from agent_core.errors import PolicyDenied


class RagWithoutArticleMemoryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.BlogAgentTests.setUp

    async def test_summary_reads_body_then_generates_answer(self):
        client = model(answer('Ready'),
            response(call('read_post_full', {'post_id': 'agent-state'}, 'body')),
            answer('State keeps durable tool results. [[agent-state]]'))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit('总结文章 agent-state')
        self.assertIn('durable tool results', reply)
        self.assertEqual(client.responses.create.await_count, 3)
        self.assertIn('durable tool results', str(client.responses.create.await_args.kwargs['input']))
        self.assertNotIn('derived_evidence', self.state['turns'][-1])

    async def test_retired_tools_denied_even_with_saved_selection(self):
        self.state['article_memory_selection'] = {'post_id': 'agent-state'}
        tools = BlogTools(SamplePosts(self.posts_dir), 'qa', self.state)
        for name in ('get_article_overview', 'read_article_section'):
            with self.assertRaises(PolicyDenied):
                await tools.execute(name, {'post_id': 'agent-state'})

    async def test_explicit_prompt_upgrade_keeps_saved_budget_and_history(self):
        import copy
        self.state['instructions'] += '\nUse get_article_overview for summaries.'
        before = copy.deepcopy(self.state)
        self.store.upgrade_prompt(self.state)
        self.assertNotIn('get_article_overview', self.state['instructions'])
        for field in ('history', 'turns', 'turn_count', 'tool_calls'):
            self.assertEqual(self.state[field], before[field])
        self.assertTrue(self.store.prompt_status(self.store.load())['current'])

    async def test_large_article_source_still_readable(self):
        (self.posts_dir / 'large.md').write_text('# Large\n' + 'padding\n' * 20000 + 'Precise tail fact.\n', encoding='utf-8')
        client = model(answer('Ready'),
            response(call('read_post', {'post_id': 'large', 'start_line': 20002, 'limit': 1}, 'tail')),
            answer('Precise tail fact. [[large]]'))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit('large文章最后一行说什么？')
        self.assertIn('Precise tail fact', reply)
        self.assertIn('Precise tail fact', self.state['tool_calls'][-1]['result']['data']['text'])
