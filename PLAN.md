# Fables: Agent-Friendly Import and Session Portability

## Vision

Fables should be a predictable session API that both humans and coding agents can operate safely.

Import means:

> Ingest conversations from any supported harness into a permanent, provider-neutral Fables library while preserving their original data, identity, completeness, and provenance.

Import does **not** mean writing files into `~/.codex`, `~/.claude`, `~/.pi`, or another harness's private storage. Once imported, sessions from Codex, Claude, Pi, VS Code, and other supported providers become discoverable and readable through the same Fables CLI, MCP server, and UI.

Three operations must remain distinct:

| Operation | Meaning |
| --- | --- |
| Import | Add external sessions to the durable Fables library. |
| Restore | Reconstruct a native session for its original harness. This is provider-specific and may not always be possible. |
| Handoff | Start a new session in another harness using selected context from an existing session. |

## Design principles

Fables should follow the qualities that make Herdr agent-friendly:

- The installed CLI is the authority for current syntax.
- Bare command groups display help and never mutate state.
- Read-only inspection precedes mutation.
- Mutating commands require explicit inputs and targets.
- Machine-readable commands return structured JSON.
- Agents consume returned opaque identifiers instead of guessing identifiers from filenames, titles, or display order.
- State, conflicts, warnings, and error conditions have precise meanings.
- Repeated operations are idempotent.
- Operations are atomic where practical.
- Human-facing display labels are never used as storage identity.
- The CLI and MCP surfaces expose equivalent concepts.
- Provider-owned storage is read-only unless a separate, explicitly requested restore operation is implemented.

## Agent discovery contract

Agents begin with:

```bash
fables --help
fables import
fables session
```

Running `fables import` without a subcommand must display the import command group's help. It must not infer an input, scan arbitrary directories, or start an import.

Commands should support `--format json`. Agent documentation should recommend JSON and require agents to parse identifiers and state from command responses.

Syntax errors should exit with status 2. Operational errors should exit with status 1 and return a structured error on stderr when JSON output was requested. Successful operations should exit with status 0.

Suggested error envelope:

```json
{
  "ok": false,
  "error": {
    "code": "import_conflict",
    "message": "One session has the same native identity but different content.",
    "details": {}
  }
}
```

## Import workflow

### 1. Inspect

Inspection is completely read-only:

```bash
fables import inspect fables-all-sessions-m2.zip --format json
```

It should:

- Resolve the exact input without recursively expanding an ambiguous path.
- Detect the input format.
- Calculate a SHA-256 digest for the complete input.
- Count sessions by provider.
- Compare candidates with the existing Fables library.
- Classify sessions as new, exact duplicates, revisions, or conflicts.
- Report whether raw records, reasoning, system context, attachments, paths, emails, or suspected secrets are present, excluded, or redacted.
- Detect whether the bundle is a sharing export or a lossless migration bundle.
- Report parse failures without dumping transcript content.
- Return the identifiers required by the next operation.
- Make no filesystem or database changes.

Example response:

```json
{
  "ok": true,
  "result": {
    "format": "fables-export-v1",
    "sha256": "sha256:...",
    "sessions": {
      "found": 21,
      "new": 18,
      "duplicates": 3,
      "revisions": 0,
      "conflicts": 0,
      "unreadable": 0
    },
    "sources": {
      "codex": 17,
      "pi": 3,
      "vscode": 1
    },
    "completeness": {
      "raw_records": false,
      "reasoning": false,
      "system_context": false,
      "attachments": false,
      "redacted": true
    },
    "warnings": [
      "This is a sharing export, not a lossless migration bundle."
    ]
  }
}
```

Inspection must avoid printing message bodies, tool outputs, secrets, or other transcript content unless a separate explicit preview command requests it.

### 2. Apply

Mutation is explicit:

```bash
fables import apply fables-all-sessions-m2.zip \
  --origin m2-work \
  --expect-sha256 sha256:... \
  --format json
```

`--expect-sha256` binds the apply operation to the input that was inspected. If the input changes, apply must stop without modifying the library.

The origin label must be supplied by the user or an unambiguous signed manifest. An agent must not invent whether an archive came from `m1-air`, `m2-work`, `m5-pro`, or `hetzner`.

Example response:

```json
{
  "ok": true,
  "result": {
    "import_id": "im_7fd...",
    "origin": "m2-work",
    "created": ["s_a12...", "s_b84..."],
    "duplicates": ["s_901..."],
    "revisions": [],
    "conflicts": [],
    "unreadable": []
  }
}
```

Agents must consume these returned identifiers. They must not derive session IDs from filenames, titles, timestamps, native IDs, or sidebar positions.

### 3. Verify

Every apply operation receives an opaque import ID:

```bash
fables import get im_7fd... --format json
```

Import state should be one of:

- `planned`: a persisted plan exists, if persisted plans are later supported.
- `applying`: the atomic import transaction is in progress.
- `complete`: all eligible sessions were committed.
- `partial`: explicitly allowed sessions were committed and failures are recorded.
- `failed`: nothing was committed.

The initial implementation should prefer all-or-nothing atomic imports. Partial imports should require an explicit future option rather than being the default.

## Session interface

Imported and live sessions should share the same read surface:

```bash
fables session list --origin m2-work --format json
fables session search "keepass setup" --format json
fables session get s_a12... --format markdown
fables session get s_a12... --format json
fables session provenance s_a12... --format json
```

The UI may display a friendly label such as:

```text
Codex · archived from m2-work
```

The CLI and MCP surfaces operate on the stable Fables session ID.

Each imported session should retain:

- Stable opaque Fables session ID.
- Original harness and source format.
- Native harness session ID and known aliases.
- Source machine or origin label.
- Original timestamps.
- Original cwd and project metadata.
- Model, effort, token, branch, and diagnostic metadata when available.
- Import ID and import timestamp.
- Input bundle digest.
- Raw-content digest.
- Normalized-content digest.
- Completeness and redaction flags.
- Duplicate, revision, or conflict relationships.
- Attachment inventory and digests when supported.

## Identity, duplicates, revisions, and conflicts

Provider-local paths and existing discovery hashes are not portable identities because paths change between machines.

Fables should use separate concepts:

- `session_id`: stable opaque Fables identifier exposed publicly.
- `native_id`: identifier assigned by the original harness.
- `origin`: machine or source system from which the data was imported.
- `raw_digest`: content identity of preserved provider data.
- `normalized_digest`: content identity of the canonical Fables representation.

Rules:

- Same raw digest: exact duplicate; skip idempotently.
- Same provider, native ID, origin, and raw digest: exact duplicate.
- Same provider and native ID with newer or differing content from the same origin: preserve as a revision; never overwrite silently.
- Same native identity from different origins with identical content: one logical session may reference both provenances.
- Same native identity with incompatible content and no safe ordering: conflict; stop by default.
- Missing native ID: identity falls back to content digest plus provenance, not title or timestamp alone.

Repeated application of the same bundle must not create additional sessions or provenance rows.

## Storage model

Proposed local layout:

```text
~/.local/share/fables/
  library.db
  objects/
    <sha256>
  imports/
    <import-id>/
      manifest.json
```

Responsibilities:

- `library.db` indexes sessions, provenance, aliases, imports, revisions, attachments, and content objects.
- `objects/` is an immutable, content-addressed object store.
- `imports/<import-id>/manifest.json` records exactly what was inspected and committed.
- The original input ZIP may optionally be retained as an object, but imported sessions must remain readable if the user's original ZIP is moved or deleted.

An import should stage content in a temporary directory, validate it, commit its database transaction, and atomically move content objects into place. Failures before commit must leave no visible partial library state.

Objects should be written with restrictive user-only permissions because transcripts may contain source code, credentials, personal information, and company material.

## Portable migration bundle

A lossless bundle should contain both original provider data and the normalized Fables representation:

```text
manifest.json
sessions/
  <portable-session-id>/
    metadata.json
    normalized.jsonl
    raw/
      <provider-specific records>
    attachments/
      <optional content-addressed attachments>
```

`normalized.jsonl` provides a stable, provider-neutral format for reading, search, export, and agent access. `raw/` remains the source of truth for future reparsing, improved parsers, audits, and possible provider-specific restoration.

For SQLite-backed providers, export only the rows and referenced attachments belonging to the selected sessions. Do not copy an entire application database when it would include unrelated sessions or secrets.

Every bundle manifest should include:

- Bundle schema and version.
- Exporter version.
- Export time.
- Source machine label.
- Session inventory.
- Per-file hashes and sizes.
- Provider and native identity.
- Completeness flags.
- Redaction flags.
- Export failures and parser diagnostics.
- Whether raw provider data and attachments are present.

## Sharing export versus migration export

The product must distinguish these explicitly:

```bash
fables export share ...
fables export migrate ...
```

### Share profile

- Designed for sending data to another person or service.
- Secret redaction enabled by default.
- Paths shortened by default.
- Reasoning excluded by default.
- System and injected context excluded by default.
- Raw records excluded by default.
- Attachments excluded unless explicitly selected.

### Private migration profile

- Designed for moving the user's own library between trusted machines.
- Raw provider data preserved.
- All normalized passages preserved, including reasoning and system context.
- Attachments preserved when supported.
- No redaction by default.
- Clearly marked as sensitive.
- Manifest records completeness and any unavailable data.

The current browser `export all` ZIP uses the sharing profile. It is a valid import input, but inspection must warn that it is not lossless when reasoning, system context, raw records, or paths were omitted or redacted.

## Supported import inputs

The importer should grow to support:

```bash
fables import inspect export.zip
fables import inspect session.jsonl
fables import inspect /path/to/provider/session
fables import inspect --home /Volumes/old-mac-home
```

Supported categories:

1. Fables multi-session ZIP exports.
2. Fables single-session JSONL archives.
3. Fables standalone HTML archives where embedded normalized data is present.
4. Individual native provider files.
5. Provider session directories.
6. Alternate home directories using the existing provider discovery layer.
7. Future remote or cloud Fables libraries through an explicit connector.

Directory traversal must be explicit and bounded. Importing multiple discovered sessions should require a bundle, an alternate home, or an explicit `--all` option. A command must not silently recurse through an arbitrary directory tree.

## Cross-harness access and handoff

Import makes every supported session readable by every agent through Fables. It does not claim that a Codex session is natively resumable by Claude or Pi.

Cross-harness continuation should be a separate handoff operation:

```bash
fables handoff s_a12... --to codex
fables handoff s_a12... --to claude
fables handoff s_a12... --to pi
```

A handoff should:

- Select relevant messages and tool outcomes.
- Include user-approved reasoning or system context only when appropriate.
- Summarize decisions, unresolved work, and relevant files.
- Preserve a provenance link to the source Fables session.
- Create a new target-harness session or a context package accepted by that harness.
- Clearly state that the target session is new rather than a continuation of native internal state.

Fables must never forge one harness's private transcript format into another harness's store.

Native restore, if implemented, should be provider-specific, conservative, and separate from import and handoff. It should require explicit user authorization and provider compatibility checks.

## MCP parity

The MCP server should expose the same conceptual operations:

- `inspect_import`
- `apply_import`
- `get_import`
- `list_sessions`
- `search_sessions`
- `get_session`
- `get_session_provenance`
- Future: `create_handoff`

MCP responses should use the same envelopes, identifiers, states, completeness flags, and error codes as the CLI.

Mutating MCP tools must:

- Require an exact inspected input and expected digest.
- Return the import ID and affected session IDs.
- Avoid transcript contents in routine status output.
- Never mutate provider-owned session stores.
- Be documented as requiring user confirmation by the Fables skill.

## Fables skill

Fables should ship an agent skill that teaches this workflow. Its core rules should be:

1. Confirm the installed CLI syntax with `fables --help` and the relevant command group.
2. Use JSON output for discovery and state-changing operations.
3. Inspect every import before applying it.
4. Report the detected format, origin, completeness, secrets warning, new sessions, duplicates, revisions, conflicts, and unreadable sessions.
5. Obtain user approval before applying an import unless the user already explicitly requested applying that exact inspected input.
6. Bind apply to the inspected SHA-256 digest.
7. Read identifiers from command responses; never infer them.
8. Do not invent source-machine labels.
9. Do not print transcript bodies or secret-bearing tool output during routine inspection.
10. Never write into native harness storage as part of import.
11. Verify the result with `fables import get` and report the final counts.
12. Treat migration bundles as sensitive data.

The skill should explain the semantic difference between live, imported, duplicate, revision, conflict, restored, and handed-off sessions.

## UI behavior

Imported sessions should appear naturally in the existing library while retaining visible provenance.

Suggested UI additions:

- Origin badge such as `archived from m2-work`.
- Filter for live versus archived sessions.
- Filter by origin machine.
- Provenance panel showing import ID, native ID, hashes, completeness, and revisions.
- Warning badge for redacted or incomplete sharing exports.
- Duplicate and revision relationships.
- Read-only import review showing counts before apply.

The UI must use the same backend import plan and apply operation as the CLI and MCP surfaces rather than implementing separate import semantics in browser code.

## Security and privacy

- Treat all transcripts, raw provider records, attachments, and migration bundles as sensitive.
- Use user-only filesystem permissions for local storage.
- Never upload during local import.
- Avoid placing transcript content in logs or ordinary error messages.
- Validate ZIP paths and reject path traversal, absolute paths, links, and decompression bombs.
- Bound total files, expanded bytes, per-file bytes, nesting, and parser work.
- Verify manifest hashes before commit.
- Reject unsupported future schema versions unless compatibility is explicit.
- Record whether best-effort secret scanning was performed; do not describe it as a security boundary.
- Preserve redaction and completeness facts rather than implying redacted data can be reconstructed.
- Never prune imported history merely because a source machine is offline or its native files were deleted.
- Keep future cloud synchronization opt-in and separate from local import.

## Proposed implementation phases

> **Implementation status:** Phases 1–3 completed on 2026-09-01. The first
> complete import release is installed and the M1/M2 archives have been
> imported and verified. Phases 4–7 remain future work.

### Phase 1: Read-only import inspection — ✅ Complete

- [x] Introduce the CLI command-group structure.
- [x] Implement `fables import inspect` for the existing multi-session ZIP.
- [x] Validate ZIP safety and `manifest.json`.
- [x] Parse session JSONL metadata without displaying transcript bodies.
- [x] Calculate input and per-session digests.
- [x] Report source counts, completeness, failures, and compatibility.
- [x] Define stable JSON success and error envelopes.

### Phase 2: Durable normalized library — ✅ Complete

- [x] Add `library.db` schema.
- [x] Add the immutable object store.
- [x] Implement stable Fables IDs.
- [x] Implement transactional `fables import apply`.
- [x] Implement duplicate, revision, and conflict rules.
- [x] Implement `fables import get`.
- [x] Add imported-session discovery to the local server.
- [x] Render imported normalized archives in the existing UI.

### Phase 3: Agent-readable library — ✅ Complete

- [x] Implement `session list`, `session search`, `session get`, and `session provenance`.
- [x] Add origin and live/archive filtering.
- [x] Extend MCP with equivalent read methods.
- [x] Return stable structured responses suitable for agents.
- [x] Ship the initial Fables skill.

### Phase 4: Lossless private migration

- Define and version the migration-bundle manifest.
- Implement `fables export migrate`.
- Preserve native provider data and normalized passages.
- Export relevant SQLite rows safely.
- Add attachment discovery and hashing.
- Import raw provider objects alongside normalized content.
- Inspect and report completeness precisely.

### Phase 5: Additional inputs

- Import single-session JSONL.
- Import standalone Fables HTML.
- Import individual provider session files and directories.
- Support `--home` discovery against mounted or copied user homes.
- Add explicit multi-session selection.

### Phase 6: Handoff

- Design context selection and provenance linking.
- Implement provider-neutral handoff packages.
- Add target-harness adapters without writing forged native history.
- Expose handoff through CLI and MCP with explicit confirmation.

### Phase 7: Provider-specific restore, only if justified

- Evaluate each harness independently.
- Restore only formats with documented, safe semantics.
- Require compatibility and collision checks.
- Keep restore outside normal import.

## Initial migration workflow for the old Macs

For the current M1 and M2 migration:

1. Keep the existing M2 sharing ZIP; do not discard it.
2. Produce and retain an M1 Fables export for immediate normalized import.
3. Implement inspect before importing either bundle.
4. Label origins explicitly as `m1-air` and `m2-work` only after confirmation.
5. Import normalized exports idempotently.
6. Later produce private migration bundles or collect the original provider stores from both laptops.
7. Re-import the lossless bundles as revisions/provenance additions rather than duplicate sessions.
8. Verify counts by origin and provider.
9. Back up the resulting Fables library according to the machine's backup policy.

## Acceptance criteria

The first complete import release is acceptable when:

- Bare `fables import` is non-mutating and displays help.
- An agent can inspect an existing Fables ZIP using one documented command.
- Inspection reports format, digest, counts, completeness, duplicates, revisions, conflicts, and warnings without leaking transcript content.
- Apply requires an explicit input, origin, and inspected digest.
- Applying an unchanged bundle twice creates no duplicate sessions.
- A changed input fails the expected-digest check without modifying the library.
- Import is atomic on failure.
- Imported sessions remain readable after the source ZIP is removed.
- Imported sessions appear in CLI, MCP, and UI using the same stable IDs.
- Provenance identifies the origin, harness, native ID, import, and content hashes.
- Existing live-session discovery continues to work.
- Import never writes to native harness directories.
- Sharing exports are visibly identified as potentially incomplete.
- Structured errors and exit statuses are documented and consistent.
- The shipped skill guides an agent through inspect, approval, apply, and verification without guessing.

## Non-goals for the initial release

- Resuming an imported session inside its original harness.
- Converting one harness's private storage format into another's.
- Automatically uploading imports to a cloud service.
- Automatically deleting source exports after import.
- Treating secret scanning as proof that an archive is safe to share.
- Merging incompatible revisions silently.
- Using titles, timestamps, paths, or UI order as canonical identity.
