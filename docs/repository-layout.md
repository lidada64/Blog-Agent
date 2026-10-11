# Repository separation — 2026-10-11

The previous remote was lidada64/Agent-Study, with Git metadata inside code/. The new product repository is lidada64/Blog-Agent, with the local Git root at D:/CST/code/Agent. Source paths remain code/ so local article and credential paths remain compatible. Existing commit history is preserved; archive/agent-study-before-blog-agent identifies the pre-reorganization head.

Archived in the current tree: agent_demo.py, check_search_progress.py, the original Skill README, legacy sample text, and the independent Tavily MCP example. The older empty FirstAgent.py/agent.py files are removed from the current tree but retained by history/tag. Core regressions, fixtures, metrics and cost/evaluation utilities remain active because they validate shared behavior.

Not uploaded: .env, local article contents, logs, conversation/Skill-library JSON, raw traces/results, caches, editor state, personal PDFs/images, refer/, research/ and mcp_test/. These files remain local. Blog/.gitkeep creates an empty input directory for fresh clones, not example articles.

The migration's Git bundle, working diff and source backup are stored locally under .blog-agent-data/repository-reorganization-20261011/. No history rewriting, forced push or production data changes are part of this operation.

Run python code/evaluate_agent.py --mode offline after install. This includes both test_agent*.py and test_blog*.py, unlike the old evaluator. Verify archive/agent_demo/agent_demo.py --help separately. Actual model calls and benchmarking require explicit credentials and have usage costs; offline checks do not.

Validation: all 97 offline tests pass, including a clean-checkout run. The published reorganization commit is 167eb60; GitHub master was updated by a normal fast-forward push after Keywall checks passed. The original remote commit history remains intact. Unpublished intermediate commits were backed up locally and combined before publishing to remove a test credential-assignment false positive.

The archive tag remains local: publishing that historical ref was blocked by Keywall findings for .env credential records in legacy history. No guard was bypassed. Review whether historical credentials need revocation and history cleanup separately; the current published tree excludes local credentials and articles.
