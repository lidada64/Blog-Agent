"""Citation discovery, evidence and repair at real read/conversation boundaries."""
import json
import asyncio
import copy
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from blog_assistant.retrieval import SamplePosts
from blog_assistant.section_citations import audit_sections
from blog_assistant.session import BlogStore, create_conversation
from blog_assistant.sections import chunks
from blog_assistant.reference_sections import ReferenceCatalog
from blog_assistant.reference_context import reference_context
from blog_assistant.convergence import RetrievalLimits
from agent_core.context import context_size
from test_blog_agent import answer, call, model, response


BODY = '# Design\n\n## Method\n\n### **Steps**\nVISIBLE_METHOD\n\n## Storage\n\n### SQL\nVISIBLE_STORAGE\n'


class ReferenceContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / 'posts'
        self.directory.mkdir()
        self.path = self.directory / 'design.md'
        self.path.write_text(BODY, encoding='utf-8')
        self.posts = SamplePosts(self.directory)
        self.store = BlogStore(self.root / 'state.json', posts_dir=self.directory)
        self.state = self.store.start('fixture', new=True, compact_threshold=500)

    def reads(self):
        return [{'name': 'read_post_full', 'status': 'completed',
                 'result': {'data': self.posts.read_full('design')}}]

    def test_parent_citation_uses_subtree_not_retrieval_chunk(self):
        result = audit_sections('Explain [[design|Method]]', self.reads(), self.posts)
        self.assertEqual(result['status'], 'located')
        self.assertIn(6, result['citations'][0]['lines'])
        self.assertNotIn(11, result['citations'][0]['lines'])
        retrieved = chunks(self.posts.snapshot('design'))
        self.assertTrue(any(c['section'] == '**Steps**' and 'VISIBLE_METHOD' in c['text'] for c in retrieved))

    def test_markdown_title_variants_resolve_without_fuzzy_title_guessing(self):
        for title in ('Steps', '**Steps**', '### **Steps**', '## Method'):
            with self.subTest(title=title):
                self.assertEqual(audit_sections('Explain [[design|' + title + ']]', self.reads(), self.posts)['status'], 'located')
        self.assertEqual(audit_sections('Explain [[design|Step]]', self.reads(), self.posts)['status'], 'invalid')

    def test_search_candidate_cannot_authorize_parent_citation(self):
        calls = [{'name': 'search_posts', 'status': 'completed', 'result': {'data': {'matches': [
            {'post_id': 'design', 'text': BODY, 'candidate_only': True}]}}}]
        self.assertEqual(audit_sections('Explain [[design|Method]]', calls, self.posts)['status'], 'invalid')

    def test_duplicate_titles_need_version_bound_ids(self):
        self.path.write_text('# Design\n## A\n### Rule\nFIRST\n## B\n### **Rule**\nSECOND\n', encoding='utf-8')
        calls = self.reads()
        result = audit_sections('Explain [[design|Rule]]', calls, self.posts)
        self.assertEqual(result['citations'][0]['reason'], 'ambiguous_section')
        entries = [e for e in ReferenceCatalog(calls, self.posts).entries() if e['section'] == 'Rule']
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]['section_id'], entries[1]['section_id'])
        for entry in entries:
            result = audit_sections('Explain ' + entry['marker'], calls, self.posts)
            self.assertEqual(result['status'], 'located')
            self.assertEqual(result['citations'][0]['section_id'], entry['section_id'])
        self.path.write_text(self.path.read_text(encoding='utf-8') + '\nChanged.\n', encoding='utf-8')
        self.assertEqual(audit_sections('Explain ' + entries[0]['marker'], self.reads(), self.posts)['status'], 'invalid')

    def test_partial_read_only_certifies_visible_subtree_lines(self):
        data = self.posts.read_full('design')
        data.update(start_line=3, end_line=6, text='\n'.join(BODY.splitlines()[2:6]))
        calls = [{'name': 'read_post', 'status': 'completed', 'result': {'data': data}}]
        result = audit_sections('Explain [[design|Method]]', calls, self.posts)
        self.assertEqual(result['status'], 'located')
        self.assertEqual(result['citations'][0]['lines'], [6])
        self.assertEqual(audit_sections('Explain [[design|Storage]]', calls, self.posts)['status'], 'invalid')
        entries = list(ReferenceCatalog(calls, self.posts).entries())
        self.assertFalse(entries[0]['complete'])
        self.assertNotIn('Storage', [e['section'] for e in entries])
        self.assertNotIn('SQL', [e['section'] for e in entries])

    def test_heading_only_fenced_fake_and_changed_source_remain_rejected(self):
        self.path.write_text('# Design\n## Empty\n\n## Real\nText\n```python\n## Fake\n```\n', encoding='utf-8')
        calls = self.reads()
        for title, reason in [('Empty', 'section_has_no_body'), ('Fake', 'unknown_section')]:
            result = audit_sections('Explain [[design|' + title + ']]', calls, self.posts)
            self.assertEqual(result['citations'][0]['reason'], reason)
        self.path.write_text('# Design\nChanged\n', encoding='utf-8')
        self.assertEqual(audit_sections('Explain [[design]]', calls, self.posts)['citations'][0]['reason'], 'stale_source')
        self.path.unlink()
        self.assertEqual(audit_sections('Explain [[design]]', calls, self.posts)['status'], 'invalid')

    async def test_actual_retry_receives_errors_and_current_reference_evidence(self):
        draft = 'FAILED_DRAFT [[design|Imaginary]]'
        client = model(answer('hello'), response(call('read_post_full', {'post_id': 'design'}, 'r')),
                       answer(draft), answer('Explanation [[design|Method]]'))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit('Explain design')
        request = client.responses.create.call_args.kwargs
        payload = json.dumps(request['input'], ensure_ascii=False)
        self.assertIn('unknown_section', payload)
        self.assertIn('FAILED_DRAFT', payload)
        self.assertIn('VISIBLE_METHOD', payload)
        self.assertIn('reference_manifest', payload)
        self.assertIn('citation_repair', payload)
        self.assertEqual(request['tools'], [])
        self.assertEqual(self.state['turns'][-1]['retrieval']['citation_retries'], 1)
        self.assertEqual(self.state['turns'][-1]['result'], 'answered_unverified')
        self.assertNotIn('FAILED_DRAFT', reply)
        self.assertNotIn('FAILED_DRAFT', str(self.state['history']))
        data = next(json.loads(i['content'].split('\n', 1)[1]) for i in request['input']
                    if isinstance(i.get('content'), str) and i['content'].startswith('引用清单'))
        self.assertIn('Imaginary', [e.get('section') for e in data['citation_repair']['errors']])
        method = next(s for s in data['reference_manifest']['sources'] if s['section'] == 'Method')
        self.assertEqual(method['visible_ranges'], [[6, 6]])
        self.assertEqual(audit_sections(method['marker'], self.reads(), self.posts)['status'], 'located')

    def test_feedback_is_bounded_and_marks_draft_and_reference_omissions(self):
        self.path.write_text('# Design\n' + '\n'.join(f'## Part {n}\nEvidence {n}' for n in range(60)), encoding='utf-8')
        context = [{'role': 'user', 'content': 'Explain design'}]
        repair = {'version': 1, 'draft': 'X' * 12000 + ' [[design|Missing]]'}
        item, usable = reference_context(context, self.reads(), self.posts, repair, 3500)
        self.assertTrue(usable)
        self.assertLessEqual(context_size(context + [item]), 3500)
        data = json.loads(item['content'].split('\n', 1)[1])
        self.assertTrue(data['citation_repair']['draft_truncated'])
        self.assertIn('unknown_section', str(data['citation_repair']['errors']))
        self.assertGreater(data['reference_manifest']['omitted'], 0)
        self.assertTrue(data['reference_manifest']['sources'])
        self.assertTrue(data['citation_repair']['draft'])
        item, usable = reference_context(context, self.reads(), self.posts, repair, context_size(context) + 20)
        self.assertFalse(usable)
        self.assertIsNone(item)

    async def test_retry_limit_and_draft_do_not_leak_into_next_turn(self):
        bad = 'FAILED_DRAFT [[design|Missing]]'
        client = model(answer('hello'), response(call('read_post_full', {'post_id': 'design'}, 'r')),
                       answer(bad), answer(bad), answer('Next [[design|Method]]'))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit('Explain design')
        self.assertEqual(self.state['turns'][-1]['result'], 'citation_unverified')
        self.assertNotIn('FAILED_DRAFT', reply)
        self.assertEqual(self.state['turns'][-1]['retrieval']['model_requests'], 3)
        await conversation.submit('Continue')
        payload = json.dumps(client.responses.create.call_args.kwargs['input'], ensure_ascii=False)
        self.assertNotIn('FAILED_DRAFT', payload)
        self.assertNotIn('citation_repair', payload)

    async def test_repair_can_copy_disambiguated_id_from_actual_request(self):
        self.path.write_text('# Design\n## A\n### Rule\nFIRST\n## B\n### Rule\nSECOND\n', encoding='utf-8')
        initial = iter([answer('hello'), response(call('read_post_full', {'post_id': 'design'}, 'r')),
                        answer('SECOND [[design|Rule]]')])
        client = model()

        async def generate(**request):
            try:
                return next(initial)
            except StopIteration:
                data = next(json.loads(i['content'].split('\n', 1)[1]) for i in request['input']
                            if isinstance(i.get('content'), str) and i['content'].startswith('引用清单'))
                self.assertEqual(data['citation_repair']['errors'][0]['reason'], 'ambiguous_section')
                source = next(s for s in data['reference_manifest']['sources']
                              if s['section'] == 'Rule' and s['visible_ranges'] == [[7, 7]])
                return answer('SECOND ' + source['marker'])

        client.responses.create.side_effect = generate
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        _, reply = await conversation.submit('Explain design')
        self.assertIn('SECOND', reply)
        self.assertIn('Rule', reply)
        self.assertNotIn('@s-', reply)
        audit = self.state['turns'][-1]['citation_audit']
        self.assertEqual(audit['status'], 'located')
        self.assertEqual(audit['citations'][0]['lines'], [7])

    async def interrupted_repair(self):
        client = model(answer('hello'), response(call('read_post_full', {'post_id': 'design'}, 'r')),
                       answer('FAILED_DRAFT [[design|Missing]]'), asyncio.CancelledError())
        conversation = create_conversation(client, self.state, self.store,
                                           retrieval_limits=RetrievalLimits(model_requests=4))
        await conversation.introduce()
        with self.assertRaises(asyncio.CancelledError):
            await conversation.submit('Explain design')
        return self.store.load()

    async def test_cancel_and_resume_keep_repair_diagnostics_and_original_budget(self):
        state = await self.interrupted_repair()
        self.assertEqual(state['turns'][-1]['retrieval']['model_requests'], 3)
        client = model(answer('Fixed [[design|Method]]'))
        conversation = create_conversation(client, state, self.store,
                                           retrieval_limits=RetrievalLimits(model_requests=99))
        await conversation.resume_turn()
        request = client.responses.create.call_args.kwargs
        self.assertIn('unknown_section', json.dumps(request['input'], ensure_ascii=False))
        self.assertEqual(request['tools'], [])
        self.assertEqual(state['turns'][-1]['retrieval']['model_requests'], 4)
        self.assertEqual(state['turns'][-1]['retrieval']['limits']['model_requests'], 4)
        # Existing budget-finalization semantics keep a partial status at the cap.
        self.assertEqual(state['turns'][-1]['citation_audit']['status'], 'located')
        self.assertIn('Fixed', state['turns'][-1]['reply'])

    async def test_permission_revocation_blocks_repair_without_model_call(self):
        state = await self.interrupted_repair()
        client = model()
        conversation = create_conversation(client, state, self.store)
        with patch.object(conversation.turn_policy.tools, 'require', side_effect=PermissionError('revoked')):
            _, reply = await conversation.resume_turn()
        self.assertEqual(client.responses.create.call_count, 0)
        self.assertNotIn('FAILED_DRAFT', reply)
        self.assertEqual(state['turns'][-1]['retrieval']['reason'], 'citation_repair_source_unavailable')

    async def test_changed_or_deleted_source_cannot_reappear_in_repair_after_resume(self):
        for action in ('changed', 'deleted'):
            with self.subTest(action=action):
                self.path.write_text(BODY, encoding='utf-8')
                self.state = self.store.start('fixture', new=True, compact_threshold=500)
                state = await self.interrupted_repair()
                if action == 'changed':
                    self.path.write_text('# Design\nNEW_UNREAD_CONTENT\n', encoding='utf-8')
                else:
                    self.path.unlink()
                client = model()
                conversation = create_conversation(client, state, self.store)
                _, reply = await conversation.resume_turn()
                self.assertEqual(client.responses.create.call_count, 0)
                self.assertNotIn('FAILED_DRAFT', reply)
                self.assertNotIn('NEW_UNREAD_CONTENT', reply)
                self.assertEqual(state['turns'][-1]['retrieval']['reason'], 'citation_repair_source_unavailable')

    async def test_insufficient_repair_context_stops_without_blind_model_retry(self):
        state = await self.interrupted_repair()
        # Keep the current evidence and memory, but leave no room for feedback.
        policy = create_conversation(model(), state, self.store).turn_policy
        turn = state['turns'][-1]
        repair = turn.pop('citation_repair')
        context = policy.assemble_context()
        without_manifest = [i for i in context if not (isinstance(i.get('content'), str) and i['content'].startswith('引用清单'))
                            and i.get('type') not in {'function_call', 'function_call_output'}]
        turn['citation_repair'] = repair
        turn['retrieval']['limits']['context_bytes'] = context_size(without_manifest) + 30
        turn['retrieval']['context_peak_bytes'] = 0
        client = model()
        conversation = create_conversation(client, state, self.store)
        await conversation.resume_turn()
        self.assertEqual(client.responses.create.call_count, 0)
        self.assertEqual(turn['retrieval']['reason'], 'citation_repair_context_budget')

    async def test_old_prompt_keeps_old_contract_until_explicit_upgrade(self):
        self.state['instructions'] = 'Saved instructions from version .13'
        self.state['prompt'] = {'version': '2026-10-13.13',
                                'hash': hashlib.sha256(self.state['instructions'].encode()).hexdigest()}
        client = model(answer('hello'), response(call('read_post_full', {'post_id': 'design'}, 'r')),
                       answer('Draft [[design|Method]]'), answer('Draft [[design|Method]]'))
        conversation = create_conversation(client, self.state, self.store)
        await conversation.introduce()
        await conversation.submit('Explain design')
        self.assertEqual(self.state['turns'][-1]['result'], 'citation_unverified')
        self.assertNotIn('citation_repair', self.state['turns'][-1])
        old_budget = copy.deepcopy(self.state['turns'][-1]['retrieval'])
        self.store.upgrade_prompt(self.state)
        self.assertEqual(self.state['turns'][-1]['retrieval'], old_budget)
        client = model(answer('Fixed [[design|Method]]'))
        await create_conversation(client, self.state, self.store).submit('Continue')
        self.assertEqual(self.state['turns'][-1]['result'], 'answered_unverified')

    def test_checkpoint_rejects_malformed_repair(self):
        from blog_assistant.convergence import validate_checkpoint
        state = {'turns': [{'citation_repair': {'version': 1, 'draft': 'bad'}}]}
        with self.assertRaises(ValueError):
            validate_checkpoint(state)


if __name__ == '__main__':
    unittest.main()
