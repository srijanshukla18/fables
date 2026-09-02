# Durable imports and session identity

Fables import ingests external conversations into a provider-neutral local
library. It never reconstructs or writes a provider's private session store.
Restore and handoff remain separate operations.

## Discovery and output contract

Confirm syntax from the installed command:

```bash
fables --help
fables import
fables session
```

Bare command groups print help and exit successfully without scanning or
mutating anything. Syntax errors exit `2`; operational errors exit `1`; success
exits `0`. With `--format json`, successful commands write:

```json
{"ok": true, "result": {}}
```

Operational errors are written to stderr:

```json
{
  "ok": false,
  "error": {"code": "import_conflict", "message": "...", "details": {}}
}
```

## Inspect, apply, verify

Inspection resolves exactly one file and is read-only. It does not create a
library database, object, temporary import plan, or provider file.

```bash
fables import inspect export.zip --origin user-supplied-label --format json
```

Inspection validates archive paths, links, encryption, file counts, nesting,
expanded sizes, compression ratios, manifest compatibility, UTF-8, and session
schemas. It calculates the complete input SHA-256 and per-session raw and
normalized digests. Routine results contain identities, hashes, counts, and
warnings—not message bodies or tool output.

The current sharing ZIP format does not record exact export checkboxes.
Therefore inspection reports detected presence and warns that absence does not
prove content never existed. Secret scanning is best effort and explicitly not
a security boundary.

Apply requires the exact input, a user-supplied origin, and the inspected
digest:

```bash
fables import apply export.zip \
  --origin user-supplied-label \
  --expect-sha256 'sha256:...' \
  --format json
```

A changed digest, unreadable session, or conflict commits no sessions. The
initial implementation is deliberately all-or-nothing; there is no implicit
partial mode. Repeating the same digest and origin returns the existing import
ID with `idempotent: true` and creates no session, import, or provenance row.

Verify with the returned opaque identifier:

```bash
fables import get 'im_...' --format json
```

## Identity rules

Titles, timestamps, paths, filenames, and display order are never canonical
identity.

- `session_id`: stable public opaque `s_...` identifier.
- `native_id`: original harness identifier, when available.
- `origin`: explicit source machine/system label.
- `raw_digest`: SHA-256 of preserved input session bytes.
- `normalized_digest`: SHA-256 of the canonical Fables archive.

Classification is deterministic:

1. Equal raw digest is an exact duplicate.
2. Equal provider/native identity and normalized content is a duplicate even if
   serialization differs.
3. Differing content with equal provider/native identity and origin is a new,
   linked revision; prior content is never overwritten.
4. Identical content from a different origin adds provenance to the same
   logical session.
5. Differing content with equal native identity across origins is a conflict;
   default apply stops.
6. Without a native ID, identity falls back to content digest and provenance.

Inspection without an origin conservatively labels differing native identities
as conflicts because it cannot safely decide whether they are same-origin
revisions. Pass `--origin` only after the user supplies it.

## Storage and atomicity

The default local layout is:

```text
~/.local/share/fables/
  library.db
  objects/<sha256-hex>
  imports/<import-id>/manifest.json
```

SQLite indexes sessions, imports, aliases, provenance, relationships,
attachments, and immutable objects. Objects hold preserved input session bytes
and canonical normalized archives. Directories use mode `0700`; transcript
objects, the database, and import manifests use user-only permissions.

Apply validates and stages content before opening a database transaction.
Content-addressed objects are moved atomically into place before the session
rows commit. A failed transaction can leave only harmless unreachable objects,
never a visible partial session library. Imported sessions remain readable if
the original ZIP is moved or deleted.

Uninstall removes application and service files but preserves a non-empty
durable library.

## Sharing versus migration

The browser's current `export all` ZIP is a **sharing export**. It supports
redaction and excludes reasoning, system context, raw records, and attachments
by default. Import accepts it but never calls it lossless.

A future private migration profile is distinct: it must preserve provider raw
records, all normalized passages, selected provider-specific rows,
attachments, per-file hashes, and exact completeness declarations. Fables will
not relabel a sharing ZIP as a migration bundle or imply omitted content can be
recovered.

## MCP

The local MCP server exposes `inspect_import`, `apply_import`, `get_import`, and
`get_session_provenance` alongside list/search/get. Import responses use the
same envelopes and error codes as the CLI. `apply_import` additionally requires
`confirmed: true` after user approval. Remote/cloud MCP remains read-only.

## Stable operational error codes

Common codes include:

- `input_not_found`, `input_unreadable`, `ambiguous_input`
- `unsupported_format`, `invalid_bundle`, `unsafe_zip`
- `origin_required`, `invalid_origin`, `invalid_digest`
- `input_changed`, `import_unreadable`, `import_conflict`
- `import_not_found`, `session_not_found`, `ambiguous_session`
- `object_missing`, `object_corrupt`, `object_collision`
- `query_required`, `library_unreadable`, `operation_failed`

Error details never intentionally include transcript bodies.
