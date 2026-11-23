# Test fixtures

`opencode-run.jsonl` and `opencode-error.jsonl` are real `opencode run --format json --auto` output from
opencode 1.19.6 (npm `opencode-ai`), recorded inside an agentbox sandbox with the prompt on stdin. There
is no Ollama in the sandbox, so the model behind them was a scripted OpenAI-compatible endpoint on
127.0.0.1 (one turn that calls `bash ls`, then one text turn; the error run had nothing listening). The
event shapes and the token counts come from opencode itself; the model's words and the token numbers are
the script's. Replace them with a stream recorded against real Ollama when one is available.

`claude-denials.jsonl` is NOT a recording: it is a small Claude Code stream-json transcript written by hand
to the shape Claude Code 2.1.306 uses for a refused tool call. The tool_result is `is_error: true` and its
text starts with "Permission for this action was denied by the Claude Code auto mode classifier. Reason: "
(auto mode) or "Permission for this action has been denied. Reason: " (other refusals), the two prefixes
the `claude` binary itself tests for; the `result` event lists them again in `permission_denials` (field
names from the binary's output schema). It has two denials, one repeated event, a plain tool error and a
non-error result quoting the text, none of which count. Replace it with a recorded stream when one is at hand.

`rex_review_harness.lua` is NOT Rex: it is a stand-in for the parts of Rex's Lua API that
`share/rex/agentbox.lua`'s `agentbox_review` action calls (`rex.exec`, `rex.ui.pick`, `rex.call`,
`rex.client.queue`, `rex.layout.block`), written from how the other actions use them. Each method prints
what it was asked. `window.list` and a session's `windows` list are assumptions about Rex's API (the action
tries both), so check the action against a real Rex.
