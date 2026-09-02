---
name: fables-import
description: Safely inspect, approve, apply, and verify Fables session imports without guessing identity or modifying provider-owned stores.
---

# Fables import workflow

Use Fables as the authority for session syntax and identity. Treat every
transcript, raw provider record, attachment, and migration bundle as sensitive.

## Required workflow

1. Confirm installed syntax with `fables --help`, `fables import`, and
   `fables session`.
2. Use `--format json` for discovery, inspection, mutation, and verification.
3. Inspect the exact input before applying it:

   ```bash
   fables import inspect INPUT --format json
   ```

   If the user has already supplied an origin, pass it to inspection for exact
   revision/conflict classification. Never invent an origin label.
4. Parse the JSON response. Report format, SHA-256, bundle kind, origin (if
   known), completeness, sensitive-data warning, new sessions, duplicates,
   revisions, conflicts, unreadable sessions, and exporter failures. Do not
   print message bodies or tool output during routine review.
5. Obtain user approval before mutation unless the user already explicitly
   requested applying that exact inspected input with that exact origin.
6. Bind apply to the returned digest:

   ```bash
   fables import apply INPUT \
     --origin USER_SUPPLIED_ORIGIN \
     --expect-sha256 'sha256:...' \
     --format json
   ```

7. Read `import_id` and all `s_...` session IDs from the response. Never derive
   IDs from filenames, titles, timestamps, native IDs, ordering, or the UI.
8. Verify using the returned import ID:

   ```bash
   fables import get 'im_...' --format json
   ```

9. Report final state and counts. A digest mismatch, conflict, or unreadable
   default import means nothing was committed.
10. Never write `~/.codex`, `~/.claude`, `~/.pi`, or another harness store as
    part of import. Import is not restore.
11. Do not describe best-effort secret scanning as a security guarantee.
12. Keep migration bundles private and protected according to the user's backup
    policy.

## Session states and relationships

- **Live:** read directly from the original harness's provider-owned store.
- **Imported/archived:** copied into the durable provider-neutral Fables
  library and readable after the source export disappears.
- **Duplicate:** identical preserved content; no additional session is created.
  A new origin may be added as provenance.
- **Revision:** differing content with the same provider/native identity and
  origin; preserved as a separate stable Fables session linked to its prior
  revision.
- **Conflict:** incompatible content with the same native identity and no safe
  ordering; apply stops rather than merging silently.
- **Restored:** a future provider-specific reconstruction in the original
  harness. It is separate from import and may be unavailable.
- **Handed off:** a new target-harness session/context package linked to a source
  Fables session. It is not continuation of private native state.

## Reading

Use returned stable IDs:

```bash
fables session list --origin ORIGIN --format json
fables session search 'query' --format json
fables session get 's_...' --format markdown
fables session provenance 's_...' --format json
```
