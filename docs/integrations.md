# Blog website integration

Blog-Agent owns Python runtime, local article retrieval, business tools, durable sessions and evaluation. The website owns identity, admin pages, formal articles, approval and future read/write APIs. Python does not directly open production SQLite.

Runtime integration is still pending. Local qa/manage are application modes, not authenticated website identities; both currently expose read-only tools. Changing a prompt cannot grant publishing permission.

On the author's workstation, Blog-Agent checkout is D:/CST/code/Agent and the website is D:/CST/code/Carrykit blog2. Before editing the website, load its AGENTS.md, docs/TASKS.md, docs/PRD.md and relevant architecture requirements. Shared product decisions are maintained in the website's docs/blog-assistant/README.md when that sibling checkout is available. Other clones use this boundary plus the implementation plan until configured for an actual site.

The website remains https://github.com/lidada64/carrykit-blog; its read-only First-version archive is not part of this repository operation.
