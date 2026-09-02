# Fables

**A local reading room for coding-agent chronicles.** Fables discovers the
conversation stores already on your machine and renders them as readable,
searchable sessions.

![library and reading room](docs/fables-screenshot.png)

## Install

Fables can install itself as a per-user background service. It starts whenever
you log in, stays bound to your own machine at `http://localhost:8321`, and does
not require `sudo`.

```bash
git clone https://github.com/srijanshukla18/fables
cd fables
./install.sh
```

The installer copies Fables to `~/.local/share/fables`, adds the control command
at `~/.local/bin/fables`, starts it immediately, and opens it in your browser.
It uses a LaunchAgent on macOS and a systemd user service on Linux. Python 3.10
or newer is the only runtime requirement.

If `~/.local/bin` is not already on your `PATH`, add it in your shell profile:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Control the installed service with:

```bash
fables open       # open the reading room
fables status     # inspect the background service
fables restart    # restart after troubleshooting
fables logs       # follow service logs
fables stop       # stop until the next login or explicit start
fables start
fables uninstall  # remove the service and installed application
```

To use another port, pass it during installation. Running the installer again
is safe and updates the installed copy in place.

```bash
./install.sh --port 3000
```

To update later, pull the latest source in the original clone and rerun the
installer:

```bash
git pull --ff-only
./install.sh
```

Fables runs only while your user session is active. It does not install a
machine-wide daemon or enable Linux user lingering. On Linux, a systemd user
session is required.

## Run without installing

No packages, build step, account, or configuration are required:

```bash
python3 serve.py
# open http://localhost:8321
```

Use `python3 serve.py 3000` to choose another port.

## Durable imports

Import copies normalized sessions into Fables' own durable library. It does
**not** write Codex, Claude, pi, or another harness's private store, and it is
separate from native restore or cross-harness handoff.

The safe workflow is inspect → approve → digest-bound apply → verify:

```bash
fables import                                      # help only; never mutates
fables import inspect export.zip --origin m2-work --format json
fables import apply export.zip \
  --origin m2-work \
  --expect-sha256 'sha256:...' \
  --format json
fables import get 'im_...' --format json
```

`--origin` is optional during inspection and required during apply. It must be
a user-supplied source label; agents must not invent one. Supplying it during
inspection allows precise revision-versus-conflict classification. Apply is
all-or-nothing, idempotent for the same digest and origin, and returns opaque
`im_...` and `s_...` identifiers which callers must consume rather than infer.

Read live and archived sessions through one command group:

```bash
fables session list --origin m2-work --format json
fables session search 'keepass setup' --format json
fables session get 's_...' --format markdown
fables session provenance 's_...' --format json
```

The initial importer accepts Fables multi-session sharing ZIPs, single-session
`fables.session.jsonl` files, and standalone Fables HTML archives. Sharing
exports are visibly marked as potentially incomplete. See
[`docs/imports.md`](docs/imports.md) for identity rules, storage, security,
errors, and the agent contract. The shipped skill is at
`skills/fables/SKILL.md` (and in the installed application directory).

## Supported sources

| Source | Local store | Status |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | Stable |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/*.jsonl` | Stable |
| pi | `~/.pi/agent/sessions/**/*.jsonl` | Stable |
| Prime Agent | `~/.prime/agent/sessions/*.jsonl` | Stable |
| Command Code | `~/.commandcode/projects/**/*.jsonl` (+ `.meta.json`) | Stable |
| Gemini CLI | `~/.gemini/tmp/*/chats/session-*.json*` | Stable |
| Cline | `~/.cline/data/tasks/*` and editor extension storage | Stable |
| Roo Code | VS Code, Cursor, and Windsurf extension storage | Stable |
| Goose | `~/Library/Application Support/Block/goose/sessions/sessions.db` or its Linux equivalent | Stable |
| Hermes Agent | `~/.hermes/state.db` plus profile databases | Stable |
| VS Code Chat | workspace, empty-window, and transferred chat sessions | Stable |
| Claude Cowork | legacy local-agent sandboxes | Experimental |
| GitHub Copilot CLI | `~/.copilot/session-state/*/events.jsonl` | Stable |
| Cursor | `state.vscdb` composer and bubble records | Experimental |
| Cursor CLI | `~/.cursor/projects/*/agent-transcripts/`, also `~/.agent/`, `~/.agents/` | Stable |
| Kimi CLI | `~/.kimi/sessions/*/*/context.jsonl` (+ `wire.jsonl` for tool calls) | Stable |
| Command Code | `~/.commandcode/projects/**/*.jsonl` (+ `.meta.json`) | Stable |
| Amp | `~/.local/share/amp/threads/*.json` | Experimental |
| Qwen Code | `~/.qwen/tmp/*/chats/*.jsonl` | Stable |
| Aider | `.aider.chat.history.md` in project roots under `~/code`, `~/projects`, `~/src`, `~/work`, `~/repos` | Stable |
| Trae | `~/Library/Application Support/Trae/User/` (workspace chats + `chat.ChatSessionStore` index) | Experimental |
| Kiro | `~/.kiro/sessions/cli/*.jsonl` (ACP logs) | Stable |
| Kilo Code | editor global-storage tasks (like Cline/Roo) | Stable |
| Zed | `~/Library/Application Support/Zed/threads/threads.db` (zstd blobs) | Experimental |
| OpenCode | current SQLite and legacy JSON storage | Experimental |

Experimental means the application's local schema is undocumented, migrating,
or both. Fables reports provider-level discovery failures instead of hiding
them. Current account-backed Cowork sessions may not have a local transcript
and therefore may not appear.

## Agents: stateless MCP server

Fables also speaks MCP (Model Context Protocol, [2026-07-28 stateless
spec](https://modelcontextprotocol.io/specification/2026-07-28)) over stdio,
so any MCP-capable agent — Codex CLI, Claude Code, pi — can list, fetch, and
search the same sessions without knowing any provider's on-disk format. The
server is stateless: no initialize handshake, no protocol sessions, every
request self-contained, so it can be restarted or load-balanced freely.

Run it directly, or through the control command after install:

```bash
python3 fables-mcp.py
fables mcp
```

### Register with every agent

```bash
python3 install-mcp.py     # or: fables mcp-install
python3 install-mcp.py --check    # report status without writing
python3 install-mcp.py --remove   # unregister everywhere
```

The installer registers fables-mcp (launched via `uv run`) with every
MCP-capable agent Fables reads: Codex CLI, Claude Code, Gemini CLI, Cursor,
OpenCode, Cline, Roo Code, VS Code Chat, Goose, Copilot CLI, Command Code
(`cmd mcp add`), Amp (`amp mcp add`), Qwen Code, Trae, Kiro, Kilo Code, Zed,
Hermes Agent, Prime Agent (HTTP endpoint + kernel skill, since its kernel only wires HTTP
MCP servers), and pi (as a pi extension bridge, since pi has no built-in MCP
client). Aider is read but not registered — it has no native MCP client
support. Each target is idempotent and gets a one-shot
`<file>.fables.bak` backup before any write.

Register with Codex CLI manually in `~/.codex/config.toml`:

```toml
[mcp_servers.fables]
command = "fables"
args = ["mcp"]
```

Tools:

- `inspect_import`, `apply_import`, and `get_import` — the same safe import
  plan as the CLI. `apply_import` requires `confirmed: true`, an explicit
  origin, and the inspected digest
- `get_session_provenance` — origins, provider/native identity, import IDs,
  hashes, completeness, attachments, and revision relationships
- `list_sessions` — newest sessions, filterable by source and by a query
  over title, cwd, opaque id, and native provider id (for example a pi UUID)
- `get_session` — the conversation as readable text. **By default only user
  and assistant messages are returned** (compact, resume-friendly context);
  pass `include_thinking: true` for reasoning blocks, `include_tools: true`
  for tool calls with arguments and tool results (the flags are independent),
  or `format: "json"` for the raw archive. `id` accepts the opaque hash
  from `list_sessions`, a native provider id, or `source:native_id`
  (for example `pi:019ffc61-...`)
- `search_sessions` — case-insensitive search over recent transcripts
  (messages by default; `include_thinking: true` also searches reasoning,
  `include_tools: true` also searches tool content). Native provider ids
  and opaque hashes match without scanning transcripts.

Resume across agents: end a session in pi, open Codex, and ask it to
`get_session` with the pi UUID (or `list_sessions` with `source: "pi"`),
then continue the work in a fresh session.

The local MCP server exposes import tools in addition to session tools. The
remote cloud remains read-only and advertises only session tools.

pi gets session tools through `~/.pi/agent/extensions/fables-mcp.ts` (installed
by install-mcp.py): `fables_list_sessions`, `fables_get_session`, and
`fables_search_sessions` appear as native pi tools on the next pi launch.

## Cloud: every machine, every harness, one library

`cloud/fables-cloud.py` hosts your sessions centrally (Google sign-in, device
tokens, sqlite), and `fables-sync.py` pushes each machine's sessions to it, so
any MCP-capable agent on any machine can reach any session. Deploy on AWS
Lightsail with `cloud/deploy-aws.sh` (Docker + Caddy auto-TLS).

```bash
# on every machine, as a daemon:
python3 fables-sync.py --url https://fables.example.com \
    --token <device-token> --watch 600
```

Sign in at `https://fables.example.com` with your Google account to mint the
device token, then point HTTP-capable agents (Prime, Zed, Kiro, Goose, …) at
`https://fables.example.com/mcp` with `Authorization: Bearer <token>`.

## Reading and searching

- Filter the library by source or search titles and projects.
- Search within a session across the complete normalized transcript, including
  passages that have not yet been rendered.
- Expand thinking, tool calls, results, injected context, and source records.
- See model changes, reasoning effort, token totals, tool status, and parse
  diagnostics where the source recorded them.
- Use browser back/forward for session history. Press `/` to search, `j`/`k`
  for the next/previous visible session, or `Cmd/Ctrl-K` for library search.

Large sessions are parsed in a Web Worker and rendered incrementally so the
reading room stays responsive.

## Sharing safely

Choose **share as html** to review and download a single offline HTML file.
Exports contain a normalized Fables archive, not the original transcript.

Choose **export all** in the library to create a ZIP with `manifest.json` and
one UTF-8 `.jsonl` file per discovered session. Each JSONL file starts with a
versioned session metadata record followed by one normalized passage per line.
After privacy options are confirmed, sessions are loaded, parsed, redacted,
compressed, and written independently in one pass through a shared parser
worker. The export uses the same privacy and redaction choices as a
single-session export. The completed archive is handed to the browser's normal
download manager, with a temporary **download ZIP again** link in the page.

Reasoning, system context, and raw records are excluded by default. A
single-session review scans for likely credentials, local paths, and email
addresses before download. The all-session export performs that scan while it
processes each session and records aggregate findings in `manifest.json`.
Tool calls and outputs are included by default because they are part of the
visible story.

Secret detection is best-effort, not a security boundary. Always inspect the
preview and treat exported files as potentially sensitive.

## Privacy and security

- The server binds only to `127.0.0.1` and rejects non-local `Host` headers.
- Browser-visible session IDs are opaque hashes; clients cannot request paths.
  MCP `get_session` also accepts native provider ids (UUIDs printed by pi,
  Cursor, and others), which are not filesystem paths.
- SQLite stores are opened read-only with bounded, session-specific queries.
- HTTP responses use restrictive content security and MIME-sniffing headers.
- Nothing is uploaded, and the runtime has no third-party dependencies.

The viewer still reads transcripts containing prompts, code, commands, file
contents, paths, and potentially secrets. Anyone with access to the local
server or an exported file can read the data shown there.

## Architecture

- `providers.py` discovers stores and converts database-backed sessions to
  bounded synthetic archives.
- `serve.py` is the local stdlib HTTP boundary.
- `fables-mcp.py` is the stateless stdio MCP server for agents.
- `fables-core.js` normalizes every provider and builds privacy-aware exports.
- `fables-worker.js` parses sessions off the main thread.
- `fables-app.js` and `fables.css` implement the dependency-free reader.

## Tests

```bash
python3 -m unittest discover -s tests -v
node --test tests/test_core.js
```

The fixtures are synthetic and sanitized; they cover every supported provider,
SQLite extraction, mutation-log replay, malformed input, tool pairing,
redaction, and HTTP boundary behavior.

## Lineage

Spiritual successor to
[claude-memory-viz](https://github.com/srijanshukla18/claude-memory-viz):
clone, one command, zero config.

MIT.
