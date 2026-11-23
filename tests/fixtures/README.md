# Test fixtures

`opencode-run.jsonl` and `opencode-error.jsonl` are real `opencode run --format json --auto` output from
opencode 1.19.6 (npm `opencode-ai`), recorded inside an agentbox sandbox with the prompt on stdin. There
is no Ollama in the sandbox, so the model behind them was a scripted OpenAI-compatible endpoint on
127.0.0.1 (one turn that calls `bash ls`, then one text turn; the error run had nothing listening). The
event shapes and the token counts come from opencode itself; the model's words and the token numbers are
the script's. Replace them with a stream recorded against real Ollama when one is available.
