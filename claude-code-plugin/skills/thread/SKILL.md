---
description: Show the Alephat thread that mirrors this Claude Code session. Use when asked for this session's Alephat link or thread.
allowed-tools: mcp__plugin_open-swe_open-swe__list_threads
---

Call `list_threads` on the Alephat MCP server with `query` set to `${CLAUDE_SESSION_ID}` and `summary_only` true.
Print the returned thread's `title` and `webUrl`.
When `items` is empty, say that this session has no Alephat thread yet: the mirror creates it on the first prompt, and needs this machine connected with `/open-swe:connect`.
Mention that a mirrored thread is private to its owner and workspace admins.
