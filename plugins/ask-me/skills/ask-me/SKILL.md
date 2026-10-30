---
name: ask-me
description: Clear every decision still waiting on you by asking them with the question tool.
disable-model-invocation: true
---

# Ask me

The Stop hook in this plugin catches most prose decision lists. This skill is the manual entry point for what it missed, or for clearing all open decisions at once.

1. Collect every decision still waiting on the user from the conversation so far, including ones raised in earlier turns.
2. Drop anything you can answer by running or reading something, and do that work now.
3. If nothing is left, say so in one line and stop.
4. Ask the rest with the question tool.
   - Claude Code: `AskUserQuestion` takes up to 4 questions per call with 2 to 4 options each. Make further calls for the rest.
   - Codex: `request_user_input` is synchronous and takes one question at a time. Never use the async variant.
   - No question tool: ask one question at a time in plain chat.
5. Give each question the context needed to decide without rereading the conversation. Put the recommended option first. Write in the user's language.
6. After the answers, continue the work.
