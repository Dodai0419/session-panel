# AGENTS.md: session-panel

Turn a Claude Code session transcript (JSONL) into one self-contained animated HTML page: the prompt, the agent, a box per tool kind, and every step replayed in order with ok/fail. Totals (tool calls, failures, retries, model requests, tokens) match agentplay's numbers. It also renders live-panel JSON configs that use text, box, line, flow and log elements with counter, lane and cycle machines.

## When to use
- The user wants to see, replay, share or visualise what a coding-agent session did.
- The user wants an animated summary of a session for a PR, a post or a slide.

## Commands (run from the repo root)
- `python session_panel.py --latest`: newest session under ~/.claude/projects; writes session-panel.html
- `python session_panel.py <session.jsonl> --out replay.html --period 1.0`: one step per second
- `python session_panel.py --config panel.json --out panel.html`: render a live-panel config
- Open the page with `?t=10` to start at second 10, `?manual` to stop the loop.

## Notes
- The page shows file paths, commands and the first prompt. Ask before sharing a page made from a private session.
