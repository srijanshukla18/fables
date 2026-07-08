# Fables 🕯️

**A reader for agent chronicles.** Every Claude Code and Codex session you've ever run
is sitting on your disk as JSONL — one JSON object per line, unreadable by mortals.
Fables turns them back into stories.

![library and reading room](docs/fables-screenshot.png)

## Quick start

```bash
git clone https://github.com/srijanshukla18/fables
cd fables
python3 serve.py
# open http://localhost:8321
```

That's it. No dependencies, no build step, no config. The server auto-discovers
every session from:

- `~/.claude/projects/**/*.jsonl` — Claude Code (including subagent transcripts)
- `~/.codex/sessions/**/*.jsonl` — Codex (both the modern enveloped format and 2025-era rollouts)

## Sharing a session

Open a session, hit **⤴ share as html**. You get a single standalone `.html` file
with the entire transcript embedded — no server, no dependencies, works offline,
opens in any browser. Send it over Slack, WhatsApp, or email.

How it works: the exported file is this very app with the raw JSONL inlined in an
inert `<script type="application/x-jsonl">` tag. The viewer notices the embedded
data on load and switches to reader-only mode.

## What you get

- **The library** — every session across both tools, sorted by recency, searchable,
  filterable by source. Titles come from Claude's `ai-title` lines and Codex's
  `thread_name_updated` events.
- **The reading room** — transcripts rendered as they deserve: prose in a book serif,
  tools in terminal mono. Amber is the human voice, coral is the machine.
  - thinking blocks collapsed (✳), expandable
  - tool calls collapsed with one-line summaries, ✓/✗ status, full input/output inside
  - slash commands, hook output, and injected context shown as quiet marginalia
  - turn markers down the spine, neutral reasoning-depth spectrum, token totals in the header
- **`{raw}`** — hover any block to peek at the raw JSONL lines behind it.
- **Search** — across the library and within a session.

## Privacy

Fables reads local agent transcripts from your machine. Those transcripts can
contain prompts, tool outputs, file paths, secrets, or pasted private data. The
local server only binds to `127.0.0.1`, and the share button creates a standalone
HTML file with the full raw JSONL embedded inside it. Review exported files before
sending them anywhere.

## Notes

- The server binds to `127.0.0.1` only; session IDs are path hashes, so the browser
  can never request arbitrary files.
- Tested against 505 real sessions across every format variant found in the
  wild: zero parse failures.
- `python3 serve.py 3000` for a custom port.

## Lineage

Spiritual successor to [claude-memory-viz](https://github.com/srijanshukla18/claude-memory-viz),
which visualized the MCP memory graph. Same philosophy: **clone, one command, zero config.**

MIT.
