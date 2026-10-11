# Blog-Agent tasks

## Current maintenance

- [x] Move Git root from code/ to Agent/, archive legacy Demo, preserve history and a local archive tag, exclude personal/runtime data, and verify all 97 tests from a clean checkout.
- [x] Rename GitHub repository from Agent-Study to Blog-Agent.
- [ ] Push reorganized master and archive/agent-study-before-blog-agent. Blocked by existing Keywall pre-push hook: credential vault must be unlocked by the user with kw login.

## Product roadmap

Current local retrieval/QA, file/keyword search, recovery and permissions have been validated. The website connection, management preparation and publishing are pending; milestones and criteria remain in [implementation plan](../blog-agent-implementation-plan.md).

- [ ] Prompt Caching adaptation and real usage/coverage observation (plan §24.1).
- [ ] Cache/compact fixed-task quality, latency and full-cost evaluation, at least five repetitions per configuration (plan §24.3).
- [ ] Selectable Subagent with independent context, parent verification, permissions/budget/failure handling and on/off comparisons (plan §24.2).
- [ ] Complete local QA evaluation, then connect the website's authenticated read-only development API (P2/P3).
- [ ] Prepare reviewable translation/summary/classification suggestions, then add approval and reliable publishing (P4/P5).
- [ ] Admin UI, batches, background worker, recovery and deployment (P6/P9).

After major changes, report Git status and remind the user to commit remaining changes as specified in AGENTS.md.
