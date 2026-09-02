import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from fables_library import Library, LibraryError


def make_session(*, native="native-1", source="pi", text="hello", title="safe title",
                 include_thinking=False, malformed=False):
    meta = {
        "type": "session", "schema": "fables.session.jsonl", "version": 1,
        "session": {"id": "old-display-id", "source": source, "title": title,
                    "project": "/Users/example/work", "mtime": 100},
        "meta": {
            "source": source, "format": source, "title": title,
            "cwd": "/Users/example/work", "branch": "main", "models": ["model"],
            "efforts": ["high"], "tokens": {"input": 2, "output": 3},
            "start": 100, "end": 101,
            "diagnostics": {"malformedLines": 0, "warnings": []},
        },
    }
    records = [meta, {"type": "item", "index": 0, "kind": "user", "text": text},
               {"type": "item", "index": 1, "kind": "assistant", "text": "answer"}]
    if include_thinking:
        records.append({"type": "item", "index": 2, "kind": "thinking", "text": "reason"})
    body = "".join(json.dumps(record) + "\n" for record in records)
    if malformed:
        body = "not json\n" + body
    return body.encode(), {
        "id": "old-display-id", "native_id": native, "source": source,
        "format": source, "title": title, "project": "/Users/example/work",
        "mtime": 101, "file": "sessions/session.jsonl",
    }


def make_zip(path: Path, *, text="hello", native="native-1", source="pi",
             malformed_header=False, extra_entry=None):
    payload, entry = make_session(native=native, source=source, text=text)
    if malformed_header:
        payload = b'{"type":"item","kind":"user","text":"private body"}\n'
    manifest = {
        "fablesExportVersion": 1, "format": "fables.session.jsonl",
        "exportedAt": "2026-01-01T00:00:00Z", "sessions": [entry],
        "failures": [], "findings": {"secrets": 0, "paths": 1, "emails": 0},
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(entry["file"], payload)
        bundle.writestr("manifest.json", json.dumps(manifest))
        if extra_entry:
            bundle.writestr(extra_entry, b"bad")
    return path


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library_root = self.root / "library"
        self.library = Library(self.library_root)

    def tearDown(self):
        self.temp.cleanup()

    def test_inspect_is_read_only_and_does_not_leak_transcript(self):
        bundle = make_zip(self.root / "export.zip", text="TOP SECRET BODY")
        result = self.library.inspect(bundle, origin="m1-air")
        self.assertFalse(self.library_root.exists())
        self.assertEqual(result["format"], "fables-export-v1")
        self.assertEqual(result["bundle_kind"], "share")
        self.assertEqual(result["sessions"]["new"], 1)
        self.assertEqual(result["sources"], {"pi": 1})
        self.assertEqual(result["content_status"]["paths"], "possibly_redacted_or_excluded")
        self.assertEqual(result["content_status"]["redaction"], "possible_profile_redaction")
        self.assertIn("sharing export", result["warnings"][0])
        self.assertNotIn("TOP SECRET BODY", json.dumps(result))
        self.assertRegex(result["sha256"], r"^sha256:[0-9a-f]{64}$")

    def test_apply_is_idempotent_and_source_independent(self):
        bundle = make_zip(self.root / "export.zip")
        inspected = self.library.inspect(bundle, origin="m1-air")
        first = self.library.apply(bundle, "m1-air", inspected["sha256"])
        sid = first["created"][0]
        bundle.unlink()
        session = self.library.get_session(sid)
        self.assertEqual(session["archive"]["items"][0]["text"], "hello")
        provenance = self.library.provenance(sid)
        self.assertEqual(provenance["provenance"][0]["origin"], "m1-air")
        manifest = self.library_root / "imports" / first["import_id"] / "manifest.json"
        self.assertTrue(manifest.exists())
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.library_root / "library.db").stat().st_mode), 0o600)
        for path in [self.library_root, self.library_root / "objects", self.library_root / "imports"]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.library_root / "objects").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        # Recreate the byte-identical archive by copying a retained byte string.
        # ZIP container metadata makes reconstructing it byte-for-byte unreliable,
        # so use a second exact file copy before deleting the first in real callers.

    def test_repeated_exact_apply_reuses_import_and_rows(self):
        bundle = make_zip(self.root / "export.zip")
        digest = self.library.inspect(bundle, origin="m1-air")["sha256"]
        first = self.library.apply(bundle, "m1-air", digest)
        second = self.library.apply(bundle, "m1-air", digest)
        self.assertEqual(first["import_id"], second["import_id"])
        self.assertTrue(second["idempotent"])
        db = sqlite3.connect(self.library_root / "library.db")
        self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM provenance").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM imports").fetchone()[0], 1)
        db.close()

    def test_digest_mismatch_has_no_side_effects(self):
        bundle = make_zip(self.root / "export.zip")
        with self.assertRaises(LibraryError) as caught:
            self.library.apply(bundle, "m1-air", "sha256:" + "0" * 64)
        self.assertEqual(caught.exception.code, "input_changed")
        self.assertFalse(self.library_root.exists())

    def test_revision_and_cross_origin_conflict_are_precise_and_atomic(self):
        first_zip = make_zip(self.root / "first.zip", text="version one")
        first_digest = self.library.inspect(first_zip, origin="m1-air")["sha256"]
        first = self.library.apply(first_zip, "m1-air", first_digest)

        revision_zip = make_zip(self.root / "revision.zip", text="version two")
        inspection = self.library.inspect(revision_zip, origin="m1-air")
        self.assertEqual(inspection["sessions"]["revisions"], 1)
        revision = self.library.apply(revision_zip, "m1-air", inspection["sha256"])
        self.assertEqual(len(revision["revisions"]), 1)
        self.assertEqual(
            self.library.provenance(revision["revisions"][0])["relationships"]["revision_of"],
            first["created"][0],
        )

        conflict_zip = make_zip(self.root / "conflict.zip", text="incompatible")
        conflict = self.library.inspect(conflict_zip, origin="m2-work")
        self.assertEqual(conflict["sessions"]["conflicts"], 1)
        before = len(self.library.list_sessions())
        with self.assertRaises(LibraryError) as caught:
            self.library.apply(conflict_zip, "m2-work", conflict["sha256"])
        self.assertEqual(caught.exception.code, "import_conflict")
        self.assertEqual(len(self.library.list_sessions()), before)

    def test_identical_content_from_another_origin_adds_only_provenance(self):
        bundle = make_zip(self.root / "export.zip")
        digest = self.library.inspect(bundle, origin="m1-air")["sha256"]
        first = self.library.apply(bundle, "m1-air", digest)
        second = self.library.apply(bundle, "m2-work", digest)
        self.assertEqual(second["created"], [])
        self.assertEqual(second["duplicates"], first["created"])
        self.assertEqual(second["provenance_added"], first["created"])
        self.assertEqual(len(self.library.list_sessions()), 1)
        origins = {row["origin"] for row in self.library.provenance(first["created"][0])["provenance"]}
        self.assertEqual(origins, {"m1-air", "m2-work"})

    def test_unreadable_default_apply_is_all_or_nothing(self):
        bundle = make_zip(self.root / "bad.zip", malformed_header=True)
        inspection = self.library.inspect(bundle, origin="m1-air")
        self.assertEqual(inspection["sessions"]["unreadable"], 1)
        with self.assertRaises(LibraryError) as caught:
            self.library.apply(bundle, "m1-air", inspection["sha256"])
        self.assertEqual(caught.exception.code, "import_unreadable")
        self.assertFalse(self.library_root.exists())

    def test_directory_input_is_not_recursed_implicitly(self):
        nested = self.root / "directory"
        nested.mkdir()
        make_zip(nested / "hidden.zip")
        with self.assertRaises(LibraryError) as caught:
            self.library.inspect(nested)
        self.assertEqual(caught.exception.code, "ambiguous_input")
        self.assertFalse(self.library_root.exists())

    def test_zip_traversal_and_links_are_rejected(self):
        traversal = make_zip(self.root / "traversal.zip", extra_entry="../escape")
        with self.assertRaises(LibraryError) as caught:
            self.library.inspect(traversal)
        self.assertEqual(caught.exception.code, "unsafe_zip")

        link = self.root / "link.zip"
        payload, entry = make_session()
        manifest = {"fablesExportVersion": 1, "format": "fables.session.jsonl", "sessions": [entry]}
        with zipfile.ZipFile(link, "w") as bundle:
            bundle.writestr(entry["file"], payload)
            bundle.writestr("manifest.json", json.dumps(manifest))
            info = zipfile.ZipInfo("sessions/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(info, "target")
        with self.assertRaises(LibraryError) as caught:
            self.library.inspect(link)
        self.assertEqual(caught.exception.code, "unsafe_zip")

    def test_high_ratio_decompression_bomb_is_rejected_before_read(self):
        bomb = self.root / "bomb.zip"
        payload, entry = make_session()
        manifest = {"fablesExportVersion": 1, "format": "fables.session.jsonl", "sessions": [entry]}
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(entry["file"], payload)
            bundle.writestr("manifest.json", json.dumps(manifest))
            bundle.writestr("attachments/bomb", b"0" * (10 * 1024 * 1024))
        with self.assertRaises(LibraryError) as caught:
            self.library.inspect(bomb)
        self.assertEqual(caught.exception.code, "unsafe_zip")

    def test_single_jsonl_and_standalone_html_inputs(self):
        payload, _entry = make_session()
        jsonl = self.root / "session.jsonl"
        jsonl.write_bytes(payload)
        self.assertEqual(self.library.inspect(jsonl)["format"], "fables-session-jsonl-v1")

        archive = {
            "fablesVersion": 2,
            "meta": {"source": "codex", "format": "codex", "title": "html"},
            "items": [{"kind": "user", "text": "from html"}],
        }
        html_path = self.root / "session.html"
        html_path.write_text(
            '<html><script type="application/json" id="embedded-data">' +
            json.dumps(archive) + "</script></html>", encoding="utf-8",
        )
        result = self.library.inspect(html_path)
        self.assertEqual(result["format"], "fables-html-v2")
        self.assertEqual(result["sources"], {"codex": 1})


if __name__ == "__main__":
    unittest.main()
