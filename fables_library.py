"""Durable, provider-neutral Fables import library.

This module owns import semantics for the CLI, MCP server, and local UI.  It
never writes provider-owned storage.  The initial adapters accept the current
Fables multi-session sharing ZIP, a single ``fables.session.jsonl`` archive,
and a standalone Fables HTML archive with embedded normalized data.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
NORMALIZED_VERSION = 2
EXPORT_FORMAT = "fables.session.jsonl"
IMPORT_MANIFEST_SCHEMA = "fables.import.v1"

MAX_ZIP_FILES = int(os.environ.get("FABLES_MAX_ZIP_FILES", "20000"))
MAX_EXPANDED_BYTES = int(os.environ.get("FABLES_MAX_EXPANDED_BYTES", str(8 * 1024**3)))
MAX_FILE_BYTES = int(os.environ.get("FABLES_MAX_FILE_BYTES", str(1024**3)))
MAX_MANIFEST_BYTES = 64 * 1024**2
MAX_COMPRESSION_RATIO = 200
MAX_JSONL_LINES = 2_000_000
SEARCH_SCAN_LIMIT = 1000

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_ORIGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-(?:proj-)?|sk-ant-|github_pat_|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.I),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b\s*[:=]\s*[\"']?[^\s\"',;]{6,}", re.I),
)
_PATH_PATTERNS = (
    re.compile(r"/Users/[^/\s\"'`]+(?=/)"),
    re.compile(r"/home/[^/\s\"'`]+(?=/)"),
    re.compile(r"\b[A-Za-z]:\\Users\\[^\\\s\"'`]+(?=\\)"),
)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_REDACTION_PATTERN = re.compile(r"\[(?:redacted secret|redacted email)\]", re.I)
_EMBEDDED_DATA = re.compile(
    r'<script\b[^>]*\bid=["\']embedded-data["\'][^>]*>(.*?)</script\s*>',
    re.I | re.S,
)


class LibraryError(Exception):
    """Operational failure with a stable JSON error code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def default_library_root() -> Path:
    configured = os.environ.get("FABLES_LIBRARY")
    return Path(configured).expanduser() if configured else Path.home() / ".local/share/fables"


def _iso(value: float | None = None) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value if value is not None else time.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise LibraryError("input_unreadable", "The exact input file could not be read.", {"reason": str(exc)}) from None
    return "sha256:" + digest.hexdigest(), size


def _hex_digest(value: str) -> str:
    match = _DIGEST.fullmatch(str(value or "").lower())
    if not match:
        raise LibraryError("invalid_digest", "A SHA-256 digest must use sha256:<64 hexadecimal characters>.")
    return match.group(1)


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _new_id(prefix: str, bytes_: int = 12) -> str:
    return prefix + secrets.token_hex(bytes_)


def validate_origin(origin: str | None) -> str:
    value = str(origin or "").strip()
    if not value:
        raise LibraryError("origin_required", "Apply requires an explicit source-machine origin label.")
    if not _ORIGIN.fullmatch(value):
        raise LibraryError(
            "invalid_origin",
            "Origin must be 1-64 characters, start with a letter or digit, and contain only letters, digits, '.', '_', or '-'.",
        )
    return value


def _safe_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else fallback
    except (TypeError, ValueError):
        return fallback


def _timestamp_seconds(value: Any) -> float:
    number = _safe_number(value)
    if number > 100_000_000_000:
        return number / 1000.0
    if number:
        return number
    if isinstance(value, str) and value:
        from datetime import datetime
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _strings(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 20:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, depth + 1)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child, depth + 1)


def _scan_sensitive(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"suspected_secrets": 0, "paths": 0, "emails": 0, "redaction_markers": 0}
    for item in items:
        for text in _strings(item):
            counts["suspected_secrets"] += sum(len(pattern.findall(text)) for pattern in _SECRET_PATTERNS)
            counts["paths"] += sum(len(pattern.findall(text)) for pattern in _PATH_PATTERNS)
            counts["emails"] += len(_EMAIL_PATTERN.findall(text))
            counts["redaction_markers"] += len(_REDACTION_PATTERN.findall(text))
    return {
        "performed": True,
        "security_boundary": False,
        **counts,
    }


def _completeness(items: list[dict[str, Any]], declared: dict[str, Any] | None = None) -> dict[str, bool]:
    declared = declared or {}
    kinds = {str(item.get("kind") or "") for item in items}
    detected_raw = any("raw" in item for item in items)
    markers = any(_REDACTION_PATTERN.search(text) for item in items for text in _strings(item))
    return {
        "raw_records": bool(declared.get("raw_records", detected_raw)),
        "reasoning": bool(declared.get("reasoning", "thinking" in kinds)),
        "system_context": bool(declared.get("system_context", "info" in kinds)),
        "attachments": bool(declared.get("attachments", False)),
        "redacted": bool(declared.get("redacted", markers)),
    }


def _plain_meta(meta: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    models = meta.get("models") if isinstance(meta.get("models"), list) else []
    efforts = meta.get("efforts") if isinstance(meta.get("efforts"), list) else []
    tokens = meta.get("tokens") if isinstance(meta.get("tokens"), dict) else {}
    diagnostics = meta.get("diagnostics") if isinstance(meta.get("diagnostics"), dict) else {}
    return {
        "source": str(meta.get("source") or fallback.get("source") or "archive").lower(),
        "format": str(meta.get("format") or fallback.get("format") or meta.get("source") or "archive"),
        "title": str(meta.get("title") or fallback.get("title") or "untitled session"),
        "cwd": str(meta.get("cwd") or fallback.get("project") or ""),
        "branch": str(meta.get("branch") or ""),
        "models": [str(value) for value in models if value is not None],
        "efforts": [str(value) for value in efforts if value is not None],
        "tokens": tokens,
        "start": meta.get("start") or "",
        "end": meta.get("end") or "",
        "mtime": meta.get("mtime") or fallback.get("mtime") or "",
        "diagnostics": diagnostics,
    }


def _parse_session_jsonl(data: bytes, fallback: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LibraryError("session_unreadable", "A session archive is not valid UTF-8.") from None
    header: dict[str, Any] | None = None
    items: list[dict[str, Any]] = []
    malformed = 0
    for number, line in enumerate(text.splitlines(), 1):
        if number > MAX_JSONL_LINES:
            raise LibraryError("session_unreadable", "A session archive exceeds the parser line limit.")
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        if record.get("type") == "session" and header is None:
            header = record
        elif record.get("type") == "item":
            clean = {key: value for key, value in record.items() if key not in {"type", "index"}}
            if clean.get("kind"):
                items.append(clean)
    if header is None or header.get("schema") != EXPORT_FORMAT or header.get("version") != 1:
        raise LibraryError("session_unreadable", "The session has no supported fables.session.jsonl v1 header.")
    meta = _plain_meta(header.get("meta") if isinstance(header.get("meta"), dict) else {}, fallback)
    session = header.get("session") if isinstance(header.get("session"), dict) else {}
    if not meta.get("mtime"):
        meta["mtime"] = session.get("mtime") or ""
    diagnostics = {"malformed_lines": malformed}
    return meta, items, {"session": session, "diagnostics": diagnostics}


def _scan_session_jsonl(stream, fallback: dict[str, Any], *,
                        raw_output=None, normalized_output=None,
                        declared: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scan normalized session JSONL with memory bounded to one record.

    The canonical archive order matches ``_canonical`` with sorted keys:
    fablesVersion, items, then meta. Optional outputs are written while hashes
    are calculated, so apply never retains a whole large session in memory.
    """
    raw_hash = hashlib.sha256()
    normalized_hash = hashlib.sha256()
    prefix = b'{"fablesVersion":2,"items":['
    normalized_hash.update(prefix)
    if normalized_output is not None:
        normalized_output.write(prefix)
    raw_size = 0
    normalized_size = len(prefix)
    header: dict[str, Any] | None = None
    item_count = 0
    malformed = 0
    kinds: set[str] = set()
    raw_records = False
    sensitive = {
        "performed": True, "security_boundary": False,
        "suspected_secrets": 0, "paths": 0, "emails": 0,
        "redaction_markers": 0,
    }
    for number, line in enumerate(stream, 1):
        if number > MAX_JSONL_LINES:
            raise LibraryError("session_unreadable", "A session archive exceeds the parser line limit.")
        raw_size += len(line)
        if raw_size > MAX_FILE_BYTES:
            raise LibraryError("session_unreadable", "A session archive exceeds the per-file limit.")
        raw_hash.update(line)
        if raw_output is not None:
            raw_output.write(line)
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        if record.get("type") == "session" and header is None:
            header = record
            continue
        if record.get("type") != "item":
            continue
        if header is None:
            malformed += 1
            continue
        item = {key: value for key, value in record.items() if key not in {"type", "index"}}
        if not item.get("kind"):
            malformed += 1
            continue
        encoded = json.dumps(item, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        if item_count:
            normalized_hash.update(b",")
            if normalized_output is not None:
                normalized_output.write(b",")
            normalized_size += 1
        normalized_hash.update(encoded)
        if normalized_output is not None:
            normalized_output.write(encoded)
        normalized_size += len(encoded)
        item_count += 1
        kinds.add(str(item.get("kind") or ""))
        raw_records = raw_records or "raw" in item
        scanned = _scan_sensitive([item])
        for key in ("suspected_secrets", "paths", "emails", "redaction_markers"):
            sensitive[key] += int(scanned[key])
        del item, record, encoded
    if header is None or header.get("schema") != EXPORT_FORMAT or header.get("version") != 1:
        raise LibraryError("session_unreadable", "The session has no supported fables.session.jsonl v1 header.")
    meta = _plain_meta(header.get("meta") if isinstance(header.get("meta"), dict) else {}, fallback)
    session = header.get("session") if isinstance(header.get("session"), dict) else {}
    if not meta.get("mtime"):
        meta["mtime"] = session.get("mtime") or ""
    suffix = b'],"meta":' + json.dumps(
        meta, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"}\n"
    normalized_hash.update(suffix)
    if normalized_output is not None:
        normalized_output.write(suffix)
    normalized_size += len(suffix)
    declared = declared or {}
    completeness = {
        "raw_records": bool(declared.get("raw_records", raw_records)),
        "reasoning": bool(declared.get("reasoning", "thinking" in kinds)),
        "system_context": bool(declared.get("system_context", "info" in kinds)),
        "attachments": bool(declared.get("attachments", False)),
        "redacted": bool(declared.get("redacted", sensitive["redaction_markers"] > 0)),
    }
    return {
        "meta": meta, "session": session,
        "raw_digest": "sha256:" + raw_hash.hexdigest(), "raw_size": raw_size,
        "normalized_digest": "sha256:" + normalized_hash.hexdigest(),
        "normalized_size": normalized_size,
        "completeness": completeness, "sensitive": sensitive,
        "diagnostics": {"malformed_lines": malformed},
    }


def _stream_candidate(index: int, stream, fallback: dict[str, Any],
                      source_ref: str, *, storage_ref: str = "",
                      raw_output=None, normalized_output=None) -> Candidate:
    scanned = _scan_session_jsonl(
        stream, fallback, raw_output=raw_output,
        normalized_output=normalized_output,
    )
    meta = scanned["meta"]
    session = scanned["session"]
    provider = str(meta.get("source") or fallback.get("source") or "archive").lower()
    native_id = str(fallback.get("native_id") or session.get("native_id") or session.get("id") or "").strip()
    aliases = []
    for value in fallback.get("aliases") or []:
        text = str(value or "").strip()
        if text and text != native_id and text not in aliases:
            aliases.append(text)
    return Candidate(
        index=index, provider=provider,
        source_format=str(meta.get("format") or fallback.get("format") or provider),
        native_id=native_id, aliases=aliases, meta=meta, items=[],
        raw_bytes=None, raw_digest=scanned["raw_digest"],
        raw_size=scanned["raw_size"], normalized_bytes=None,
        normalized_digest=scanned["normalized_digest"],
        normalized_size=scanned["normalized_size"],
        completeness=scanned["completeness"], sensitive=scanned["sensitive"],
        source_ref=source_ref, storage_ref=storage_ref, parser_kind="jsonl",
    )


def _parse_embedded_archive(data: bytes, fallback: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    try:
        document = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LibraryError("session_unreadable", "The standalone archive is not valid UTF-8.") from None
    match = _EMBEDDED_DATA.search(document)
    if not match:
        raise LibraryError("session_unreadable", "The HTML file has no Fables embedded-data archive.")
    try:
        archive = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        raise LibraryError("session_unreadable", "The HTML embedded-data archive is invalid JSON.") from None
    return _parse_archive_object(archive, fallback)


def _parse_archive_object(archive: Any, fallback: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(archive, dict) or archive.get("fablesVersion") != 2 or not isinstance(archive.get("items"), list):
        raise LibraryError("session_unreadable", "The normalized archive schema is unsupported.")
    items = [dict(item) for item in archive["items"] if isinstance(item, dict) and item.get("kind")]
    meta = _plain_meta(archive.get("meta") if isinstance(archive.get("meta"), dict) else {}, fallback)
    return meta, items, {"session": {}, "diagnostics": {"malformed_lines": 0}}


@dataclass
class Candidate:
    index: int
    provider: str
    source_format: str
    native_id: str
    aliases: list[str]
    meta: dict[str, Any]
    items: list[dict[str, Any]]
    raw_bytes: bytes | None
    raw_digest: str
    raw_size: int
    normalized_bytes: bytes | None
    normalized_digest: str
    normalized_size: int
    completeness: dict[str, bool]
    sensitive: dict[str, Any]
    source_ref: str
    storage_ref: str = ""
    parser_kind: str = "jsonl"
    action: str = "new"
    target_session_id: str | None = None
    target_candidate: int | None = None
    revision_session_id: str | None = None
    revision_candidate: int | None = None
    conflict: dict[str, Any] | None = None

    def safe_summary(self) -> dict[str, Any]:
        value = {
            "candidate_id": f"c_{self.index + 1:04d}",
            "provider": self.provider,
            "native_id": self.native_id or None,
            "raw_digest": self.raw_digest,
            "normalized_digest": self.normalized_digest,
            "classification": self.action,
        }
        if self.target_session_id:
            value["existing_session_id"] = self.target_session_id
        return value


@dataclass
class ParsedInput:
    path: Path
    digest: str
    size: int
    format: str
    bundle_kind: str
    exported_at: str
    candidates: list[Candidate]
    unreadable: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    export_failures: int = 0
    manifest_findings: dict[str, int] = field(default_factory=dict)


def _candidate(index: int, raw: bytes, fallback: dict[str, Any], source_ref: str,
               parser=_parse_session_jsonl, declared: dict[str, Any] | None = None,
               storage_ref: str = "", parser_kind: str = "jsonl") -> Candidate:
    meta, items, extra = parser(raw, fallback)
    session_meta = extra.get("session") if isinstance(extra.get("session"), dict) else {}
    provider = str(meta.get("source") or fallback.get("source") or "archive").lower()
    native_id = str(fallback.get("native_id") or session_meta.get("native_id") or session_meta.get("id") or "").strip()
    aliases: list[str] = []
    for value in fallback.get("aliases") or []:
        text = str(value or "").strip()
        if text and text != native_id and text not in aliases:
            aliases.append(text)
    normalized = _canonical({"fablesVersion": NORMALIZED_VERSION, "meta": meta, "items": items})
    completeness = _completeness(items, declared)
    return Candidate(
        index=index,
        provider=provider,
        source_format=str(meta.get("format") or fallback.get("format") or provider),
        native_id=native_id,
        aliases=aliases,
        meta=meta,
        items=items,
        raw_bytes=raw,
        raw_digest=_digest(raw),
        raw_size=len(raw),
        normalized_bytes=normalized,
        normalized_digest=_digest(normalized),
        normalized_size=len(normalized),
        completeness=completeness,
        sensitive=_scan_sensitive(items),
        source_ref=source_ref,
        storage_ref=storage_ref,
        parser_kind=parser_kind,
    )


def _validate_zip_name(name: str) -> None:
    if (not name or "\x00" in name or "\\" in name or name.startswith("/")
            or re.match(r"^[A-Za-z]:", name)):
        raise LibraryError("unsafe_zip", "The ZIP contains an unsafe entry path.")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise LibraryError("unsafe_zip", "The ZIP contains an unsafe entry path.")
    if len(parts) > 20:
        raise LibraryError("unsafe_zip", "The ZIP entry nesting limit was exceeded.")


def _zip_entries(bundle: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = bundle.infolist()
    if len(infos) > MAX_ZIP_FILES:
        raise LibraryError("unsafe_zip", "The ZIP file-count limit was exceeded.", {"limit": MAX_ZIP_FILES})
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        _validate_zip_name(info.filename)
        if info.filename in result:
            raise LibraryError("unsafe_zip", "The ZIP contains duplicate entry names.")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise LibraryError("unsafe_zip", "ZIP links are not allowed.")
        if info.flag_bits & 0x1:
            raise LibraryError("unsafe_zip", "Encrypted ZIP entries are not supported.")
        if info.file_size > MAX_FILE_BYTES:
            raise LibraryError("unsafe_zip", "A ZIP entry exceeds the expanded per-file limit.")
        if info.file_size > 1024 * 1024 and info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise LibraryError("unsafe_zip", "A ZIP entry exceeds the compression-ratio limit.")
        total += info.file_size
        if total > MAX_EXPANDED_BYTES:
            raise LibraryError("unsafe_zip", "The ZIP exceeds the total expanded-size limit.")
        result[info.filename] = info
    return result


def _zip_read(bundle: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    try:
        with bundle.open(info) as stream:
            data = stream.read(limit + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise LibraryError("invalid_bundle", "A ZIP entry failed integrity validation.") from None
    if len(data) > limit:
        raise LibraryError("unsafe_zip", "A ZIP entry exceeded its expanded-size limit.")
    return data


def _parse_zip(path: Path, digest: str, size: int) -> ParsedInput:
    try:
        bundle = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise LibraryError("invalid_bundle", "The input is not a valid ZIP archive.") from None
    with bundle:
        entries = _zip_entries(bundle)
        manifest_info = entries.get("manifest.json")
        if manifest_info is None:
            raise LibraryError("invalid_bundle", "The ZIP has no root manifest.json.")
        try:
            manifest = json.loads(_zip_read(bundle, manifest_info, MAX_MANIFEST_BYTES))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LibraryError("invalid_bundle", "The ZIP manifest is invalid JSON.") from None
        if not isinstance(manifest, dict):
            raise LibraryError("invalid_bundle", "The ZIP manifest must be an object.")
        if manifest.get("fablesExportVersion") != 1 or manifest.get("format") != EXPORT_FORMAT:
            raise LibraryError(
                "unsupported_format",
                "Only fables-export-v1 multi-session ZIPs are supported by this release.",
                {"version": manifest.get("fablesExportVersion"), "format": manifest.get("format")},
            )
        inventory = manifest.get("sessions")
        if not isinstance(inventory, list):
            raise LibraryError("invalid_bundle", "The ZIP manifest sessions field must be an array.")
        candidates: list[Candidate] = []
        unreadable: list[dict[str, Any]] = []
        for index, record in enumerate(inventory):
            safe = {"candidate_id": f"c_{index + 1:04d}"}
            if not isinstance(record, dict) or not isinstance(record.get("file"), str):
                unreadable.append({**safe, "code": "invalid_inventory"})
                continue
            info = entries.get(record["file"])
            if info is None:
                unreadable.append({**safe, "code": "missing_session_file"})
                continue
            native = str(record.get("native_id") or record.get("sessionId") or "")
            aliases = [str(record["id"])] if record.get("id") else []
            fallback = {
                "source": record.get("source"),
                "format": record.get("format"),
                "title": record.get("title"),
                "project": record.get("project"),
                "mtime": record.get("mtime"),
                "native_id": native,
                "aliases": aliases,
            }
            try:
                with bundle.open(info) as stream:
                    candidate = _stream_candidate(
                        index, stream, fallback, safe["candidate_id"],
                        storage_ref=record["file"],
                    )
                candidates.append(candidate)
            except (LibraryError, zipfile.BadZipFile, RuntimeError) as exc:
                code = exc.code if isinstance(exc, LibraryError) else "invalid_bundle"
                unreadable.append({**safe, "provider": record.get("source"), "native_id": native or None, "code": code})
        failures = manifest.get("failures") if isinstance(manifest.get("failures"), list) else []
        findings = manifest.get("findings") if isinstance(manifest.get("findings"), dict) else {}
        warnings = [
            "This is a sharing export, not a lossless migration bundle. Reasoning, system context, raw records, paths, or attachments may have been excluded or redacted."
        ]
        warnings.append("The v1 sharing manifest does not record exact export options; absence cannot prove that content never existed.")
        if failures:
            warnings.append(f"The exporter recorded {len(failures)} session failure(s) that are not present in this bundle.")
        return ParsedInput(
            path=path, digest=digest, size=size, format="fables-export-v1",
            bundle_kind="share", exported_at=str(manifest.get("exportedAt") or ""),
            candidates=candidates, unreadable=unreadable, warnings=warnings,
            export_failures=len(failures),
            manifest_findings={key: int(findings.get(key) or 0) for key in ("secrets", "paths", "emails")},
        )


def parse_input(input_path: str | Path) -> ParsedInput:
    """Resolve and parse one exact input path without recursive discovery."""
    path = Path(input_path).expanduser()
    if not path.exists():
        raise LibraryError("input_not_found", "The exact input path does not exist.", {"path": str(path)})
    if not path.is_file():
        raise LibraryError("ambiguous_input", "This command requires one exact file; directories are not traversed implicitly.")
    path = path.resolve()
    digest, size = _digest_file(path)
    try:
        with path.open("rb") as stream:
            prefix = stream.read(64)
    except OSError:
        raise LibraryError("input_unreadable", "The exact input file could not be read.") from None
    if prefix.startswith(b"PK\x03\x04"):
        return _parse_zip(path, digest, size)
    if size > MAX_FILE_BYTES:
        raise LibraryError("input_too_large", "A non-ZIP input exceeds the per-file import limit.")
    fallback: dict[str, Any] = {}
    if b"<html" in prefix.lower() or path.suffix.lower() in {".html", ".htm"}:
        raw = path.read_bytes()
        candidate = _candidate(
            0, raw, fallback, "c_0001", parser=_parse_embedded_archive,
            storage_ref=str(path), parser_kind="html",
        )
        candidate.raw_bytes = None
        candidate.normalized_bytes = None
        candidate.items = []
        return ParsedInput(
            path, digest, size, "fables-html-v2", "share", "", [candidate],
            warnings=["This is a sharing archive; omitted or redacted content cannot be reconstructed."],
        )
    with path.open("rb") as stream:
        candidate = _stream_candidate(
            0, stream, fallback, "c_0001", storage_ref=str(path),
        )
    return ParsedInput(
        path, digest, size, "fables-session-jsonl-v1", "share", "", [candidate],
        warnings=["This is a normalized sharing archive, not a lossless provider migration bundle."],
    )


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS library_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
  import_id TEXT PRIMARY KEY,
  origin TEXT NOT NULL,
  input_path TEXT NOT NULL,
  input_name TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  input_format TEXT NOT NULL,
  bundle_kind TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('planned','applying','complete','partial','failed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  manifest_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  UNIQUE(origin, input_digest)
);
CREATE TABLE IF NOT EXISTS objects (
  digest TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  source_format TEXT NOT NULL,
  native_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  cwd TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  models_json TEXT NOT NULL DEFAULT '[]',
  efforts_json TEXT NOT NULL DEFAULT '[]',
  tokens_json TEXT NOT NULL DEFAULT '{}',
  diagnostics_json TEXT NOT NULL DEFAULT '{}',
  start_value TEXT NOT NULL DEFAULT '',
  end_value TEXT NOT NULL DEFAULT '',
  mtime REAL NOT NULL DEFAULT 0,
  raw_digest TEXT NOT NULL,
  normalized_digest TEXT NOT NULL,
  raw_object TEXT NOT NULL REFERENCES objects(digest),
  normalized_object TEXT NOT NULL REFERENCES objects(digest),
  completeness_json TEXT NOT NULL,
  sensitive_json TEXT NOT NULL,
  primary_origin TEXT NOT NULL,
  import_id TEXT NOT NULL REFERENCES imports(import_id),
  imported_at TEXT NOT NULL,
  revision_of TEXT REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS sessions_native ON sessions(provider, native_id);
CREATE INDEX IF NOT EXISTS sessions_raw ON sessions(raw_digest);
CREATE INDEX IF NOT EXISTS sessions_normalized ON sessions(normalized_digest);
CREATE INDEX IF NOT EXISTS sessions_origin ON sessions(primary_origin);
CREATE TABLE IF NOT EXISTS provenance (
  provenance_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  provider TEXT NOT NULL,
  native_id TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  raw_digest TEXT NOT NULL,
  normalized_digest TEXT NOT NULL,
  import_id TEXT NOT NULL REFERENCES imports(import_id),
  imported_at TEXT NOT NULL,
  UNIQUE(session_id, provider, native_id, origin, raw_digest)
);
CREATE INDEX IF NOT EXISTS provenance_origin ON provenance(origin);
CREATE TABLE IF NOT EXISTS aliases (
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  provider TEXT NOT NULL,
  alias TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'native',
  PRIMARY KEY(session_id, provider, alias)
);
CREATE TABLE IF NOT EXISTS relationships (
  left_session_id TEXT NOT NULL REFERENCES sessions(session_id),
  right_session_id TEXT NOT NULL REFERENCES sessions(session_id),
  kind TEXT NOT NULL CHECK(kind IN ('revision','duplicate','conflict','handoff')),
  import_id TEXT REFERENCES imports(import_id),
  PRIMARY KEY(left_session_id, right_session_id, kind)
);
CREATE TABLE IF NOT EXISTS attachments (
  attachment_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  name TEXT NOT NULL,
  media_type TEXT NOT NULL DEFAULT '',
  digest TEXT NOT NULL,
  object_digest TEXT REFERENCES objects(digest),
  size INTEGER NOT NULL DEFAULT 0,
  available INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass
class _Known:
    session_id: str | None
    candidate: int | None
    provider: str
    native_id: str
    raw_digest: str
    normalized_digest: str
    origins: set[str]
    imported_at: str = ""


class Library:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser() if root else default_library_root()
        self.db_path = self.root / "library.db"
        self.objects_dir = self.root / "objects"
        self.imports_dir = self.root / "imports"

    def _connect_read(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        try:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise LibraryError("library_unreadable", "The Fables library database could not be opened.", {"reason": str(exc)}) from None
        db.row_factory = sqlite3.Row
        return db

    def _connect_write(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.objects_dir.mkdir(exist_ok=True, mode=0o700)
        self.imports_dir.mkdir(exist_ok=True, mode=0o700)
        for directory in (self.root, self.objects_dir, self.imports_dir):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        if not self.db_path.exists():
            try:
                fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        db.execute("INSERT OR REPLACE INTO library_meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        db.commit()
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        return db

    def _known(self, db: sqlite3.Connection | None) -> list[_Known]:
        if db is None:
            return []
        origins: dict[str, set[str]] = {}
        for row in db.execute("SELECT session_id, origin FROM provenance"):
            origins.setdefault(row["session_id"], set()).add(row["origin"])
        return [
            _Known(
                row["session_id"], None, row["provider"], row["native_id"],
                row["raw_digest"], row["normalized_digest"],
                origins.get(row["session_id"], {row["primary_origin"]}), row["imported_at"],
            )
            for row in db.execute("SELECT session_id,provider,native_id,raw_digest,normalized_digest,primary_origin,imported_at FROM sessions")
        ]

    def _classify(self, candidates: list[Candidate], origin: str | None) -> list[dict[str, Any]]:
        db = self._connect_read()
        try:
            known = self._known(db)
        finally:
            if db is not None:
                db.close()
        conflicts: list[dict[str, Any]] = []
        for candidate in candidates:
            duplicate = next((item for item in known if item.raw_digest == candidate.raw_digest), None)
            if duplicate is None and candidate.native_id:
                duplicate = next((
                    item for item in known
                    if item.provider == candidate.provider
                    and item.native_id == candidate.native_id
                    and item.normalized_digest == candidate.normalized_digest
                ), None)
            if duplicate is not None:
                candidate.action = "duplicate"
                candidate.target_session_id = duplicate.session_id
                candidate.target_candidate = duplicate.candidate
            else:
                identity = [
                    item for item in known
                    if candidate.native_id and item.provider == candidate.provider
                    and item.native_id == candidate.native_id
                ]
                same_origin = [item for item in identity if origin and origin in item.origins]
                if same_origin:
                    latest = sorted(same_origin, key=lambda item: item.imported_at)[-1]
                    candidate.action = "revision"
                    candidate.revision_session_id = latest.session_id
                    candidate.revision_candidate = latest.candidate
                elif identity:
                    candidate.action = "conflict"
                    existing = identity[0]
                    candidate.conflict = {
                        "candidate_id": f"c_{candidate.index + 1:04d}",
                        "provider": candidate.provider,
                        "native_id": candidate.native_id,
                        "existing_session_id": existing.session_id,
                        "existing_origins": sorted(existing.origins),
                        "reason": "same native identity has incompatible content and no safe same-origin ordering",
                        "origin_required_for_precise_classification": origin is None,
                    }
                    conflicts.append(candidate.conflict)
                else:
                    candidate.action = "new"
            if candidate.action in {"new", "revision"}:
                known.append(_Known(
                    None, candidate.index, candidate.provider, candidate.native_id,
                    candidate.raw_digest, candidate.normalized_digest,
                    {origin} if origin else set(), f"candidate-{candidate.index:08d}",
                ))
        return conflicts

    def inspect(self, input_path: str | Path, origin: str | None = None) -> dict[str, Any]:
        if origin is not None:
            origin = validate_origin(origin)
        parsed = parse_input(input_path)
        conflicts = self._classify(parsed.candidates, origin)
        counts = {"new": 0, "duplicates": 0, "revisions": 0, "conflicts": 0}
        sources: dict[str, int] = {}
        complete = {"raw_records": False, "reasoning": False, "system_context": False, "attachments": False, "redacted": False}
        sensitive = {"performed": True, "security_boundary": False, "suspected_secrets": 0, "paths": 0, "emails": 0, "redaction_markers": 0}
        for candidate in parsed.candidates:
            key = candidate.action + "s" if candidate.action != "new" else "new"
            counts[key] += 1
            sources[candidate.provider] = sources.get(candidate.provider, 0) + 1
            for name, value in candidate.completeness.items():
                complete[name] = complete[name] or value
            for name in ("suspected_secrets", "paths", "emails", "redaction_markers"):
                sensitive[name] += int(candidate.sensitive[name])
        for unreadable in parsed.unreadable:
            provider = str(unreadable.get("provider") or "").lower()
            if provider:
                sources[provider] = sources.get(provider, 0) + 1
        warnings = list(parsed.warnings)
        if conflicts and origin is None:
            warnings.append("Origin was not supplied for inspection; differing native identities are conservatively classified as conflicts. Re-run inspect with --origin for precise revision classification.")
        if parsed.unreadable:
            warnings.append(f"{len(parsed.unreadable)} session(s) could not be parsed; default apply is all-or-nothing.")
        def content_state(present: bool, manifest_count: int = 0) -> str:
            if present:
                return "present"
            if parsed.bundle_kind == "share" and manifest_count:
                return "possibly_redacted_or_excluded"
            return "not_detected"

        content_status = {
            "raw_records": content_state(complete["raw_records"]),
            "reasoning": content_state(complete["reasoning"]),
            "system_context": content_state(complete["system_context"]),
            "attachments": content_state(complete["attachments"]),
            "paths": content_state(sensitive["paths"] > 0, parsed.manifest_findings.get("paths", 0)),
            "emails": content_state(sensitive["emails"] > 0, parsed.manifest_findings.get("emails", 0)),
            "suspected_secrets": content_state(
                sensitive["suspected_secrets"] > 0,
                parsed.manifest_findings.get("secrets", 0),
            ),
            "redaction": (
                "detected" if sensitive["redaction_markers"] else
                "possible_profile_redaction" if parsed.bundle_kind == "share" else
                "not_detected"
            ),
        }
        return {
            "format": parsed.format,
            "bundle_kind": parsed.bundle_kind,
            "sha256": parsed.digest,
            "bytes": parsed.size,
            "classification_origin": origin,
            "sessions": {
                "found": len(parsed.candidates) + len(parsed.unreadable),
                **counts,
                "unreadable": len(parsed.unreadable),
                "export_failures": parsed.export_failures,
            },
            "sources": dict(sorted(sources.items())),
            "completeness": complete,
            "content_status": content_status,
            "sensitive": sensitive,
            "manifest_findings": parsed.manifest_findings,
            "warnings": warnings,
            "candidates": [candidate.safe_summary() for candidate in parsed.candidates],
            "conflict_details": conflicts,
            "unreadable_details": parsed.unreadable,
        }

    def _write_staged(self, directory: Path, digest: str, data: bytes) -> Path:
        hex_digest = _hex_digest(digest)
        path = directory / hex_digest
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def _install_object(self, staged: Path, digest: str) -> None:
        destination = self.objects_dir / _hex_digest(digest)
        if destination.exists():
            if _digest(destination.read_bytes()) != digest:
                raise LibraryError("object_collision", "An immutable object path contains unexpected content.")
            staged.unlink(missing_ok=True)
            return
        os.replace(staged, destination)
        destination.chmod(0o600)

    @staticmethod
    def _stage_streamed_candidate(parsed: ParsedInput, candidate: Candidate,
                                  staging: Path, needed: list[str]) -> dict[str, Path]:
        """Stage JSONL raw + normalized objects in one bounded-memory pass."""
        outputs: dict[str, Any] = {}
        paths: dict[str, Path] = {}
        try:
            for digest in needed:
                path = staging / _hex_digest(digest)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                handle = os.fdopen(fd, "wb")
                outputs[digest] = handle
                paths[digest] = path
            raw_output = outputs.get(candidate.raw_digest)
            normalized_output = outputs.get(candidate.normalized_digest)
            fallback = {
                "source": candidate.provider, "format": candidate.source_format,
                "title": candidate.meta.get("title"), "project": candidate.meta.get("cwd"),
                "mtime": candidate.meta.get("mtime"),
                "native_id": candidate.native_id, "aliases": candidate.aliases,
            }
            if parsed.format == "fables-export-v1":
                try:
                    with zipfile.ZipFile(parsed.path) as bundle:
                        with bundle.open(candidate.storage_ref) as source:
                            loaded = _stream_candidate(
                                candidate.index, source, fallback, candidate.source_ref,
                                storage_ref=candidate.storage_ref,
                                raw_output=raw_output, normalized_output=normalized_output,
                            )
                except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
                    raise LibraryError("input_changed", "The input changed while session content was being staged.") from None
            else:
                with parsed.path.open("rb") as source:
                    loaded = _stream_candidate(
                        candidate.index, source, fallback, candidate.source_ref,
                        storage_ref=candidate.storage_ref,
                        raw_output=raw_output, normalized_output=normalized_output,
                    )
            if loaded.raw_digest != candidate.raw_digest or loaded.normalized_digest != candidate.normalized_digest:
                raise LibraryError("input_changed", "The input changed while session content was being staged.")
            for handle in outputs.values():
                handle.flush()
                os.fsync(handle.fileno())
            return paths
        except Exception:
            for handle in outputs.values():
                try:
                    handle.close()
                except OSError:
                    pass
            for path in paths.values():
                path.unlink(missing_ok=True)
            raise
        finally:
            for handle in outputs.values():
                if not handle.closed:
                    handle.close()
            for path in paths.values():
                if path.exists():
                    path.chmod(0o600)

    @staticmethod
    def _materialize(parsed: ParsedInput, candidate: Candidate) -> tuple[bytes, bytes]:
        if parsed.format == "fables-export-v1":
            try:
                with zipfile.ZipFile(parsed.path) as bundle:
                    info = bundle.getinfo(candidate.storage_ref)
                    raw = _zip_read(bundle, info, MAX_FILE_BYTES)
            except (OSError, KeyError, zipfile.BadZipFile):
                raise LibraryError("input_changed", "The input changed while session content was being staged.") from None
        else:
            try:
                raw = parsed.path.read_bytes()
            except OSError:
                raise LibraryError("input_changed", "The input changed while session content was being staged.") from None
        fallback = {
            "source": candidate.provider,
            "format": candidate.source_format,
            "title": candidate.meta.get("title"),
            "project": candidate.meta.get("cwd"),
            "mtime": candidate.meta.get("mtime"),
            "native_id": candidate.native_id,
            "aliases": candidate.aliases,
        }
        parser = _parse_embedded_archive if candidate.parser_kind == "html" else _parse_session_jsonl
        loaded = _candidate(candidate.index, raw, fallback, candidate.source_ref, parser=parser)
        if loaded.raw_digest != candidate.raw_digest or loaded.normalized_digest != candidate.normalized_digest:
            raise LibraryError("input_changed", "The input changed while session content was being staged.")
        assert loaded.raw_bytes is not None and loaded.normalized_bytes is not None
        return loaded.raw_bytes, loaded.normalized_bytes

    @staticmethod
    def _resolve_candidate_id(candidate: Candidate, assigned: dict[int, str]) -> str | None:
        if candidate.target_session_id:
            return candidate.target_session_id
        if candidate.target_candidate is not None:
            return assigned.get(candidate.target_candidate)
        return None

    def apply(self, input_path: str | Path, origin: str, expect_sha256: str) -> dict[str, Any]:
        origin = validate_origin(origin)
        expected = "sha256:" + _hex_digest(expect_sha256)
        path = Path(input_path).expanduser()
        if not path.exists() or not path.is_file():
            raise LibraryError("input_not_found", "The exact input file does not exist.")
        actual, _size = _digest_file(path.resolve())
        if actual != expected:
            raise LibraryError(
                "input_changed",
                "The input SHA-256 no longer matches the inspected digest; nothing was modified.",
                {"expected": expected, "actual": actual},
            )
        existing_db = self._connect_read()
        if existing_db is not None:
            try:
                existing = existing_db.execute(
                    "SELECT result_json FROM imports WHERE origin=? AND input_digest=? AND state='complete'",
                    (origin, actual),
                ).fetchone()
                if existing:
                    result = json.loads(existing["result_json"])
                    result["idempotent"] = True
                    return result
            finally:
                existing_db.close()
        parsed = parse_input(path)
        if parsed.digest != expected:
            raise LibraryError("input_changed", "The input changed while it was being parsed; nothing was modified.")
        conflicts = self._classify(parsed.candidates, origin)
        if parsed.unreadable:
            raise LibraryError(
                "import_unreadable",
                "The bundle contains unreadable sessions; the all-or-nothing import was not applied.",
                {"count": len(parsed.unreadable), "sessions": parsed.unreadable},
            )
        if conflicts:
            raise LibraryError(
                "import_conflict",
                "One or more sessions have the same native identity but incompatible content; nothing was modified.",
                {"count": len(conflicts), "conflicts": conflicts},
            )
        import_id = _new_id("im_", 10)
        imported_at = _iso()
        assigned = {
            candidate.index: _new_id("s_", 12)
            for candidate in parsed.candidates if candidate.action in {"new", "revision"}
        }
        created: list[str] = []
        revisions: list[str] = []
        duplicates: list[str] = []
        provenance_added: list[str] = []
        session_manifest: list[dict[str, Any]] = []
        for candidate in parsed.candidates:
            if candidate.action in {"new", "revision"}:
                sid = assigned[candidate.index]
                (revisions if candidate.action == "revision" else created).append(sid)
            else:
                sid = self._resolve_candidate_id(candidate, assigned)
                if sid:
                    duplicates.append(sid)
            session_manifest.append({
                "candidate_id": f"c_{candidate.index + 1:04d}",
                "session_id": sid,
                "action": candidate.action,
                "provider": candidate.provider,
                "native_id": candidate.native_id or None,
                "raw_digest": candidate.raw_digest,
                "normalized_digest": candidate.normalized_digest,
                "completeness": candidate.completeness,
            })
        result = {
            "import_id": import_id,
            "state": "complete",
            "origin": origin,
            "input_sha256": actual,
            "created": created,
            "duplicates": list(dict.fromkeys(duplicates)),
            "revisions": revisions,
            "provenance_added": provenance_added,
            "conflicts": [],
            "unreadable": [],
            "idempotent": False,
        }
        manifest = {
            "schema": IMPORT_MANIFEST_SCHEMA,
            "version": 1,
            "import_id": import_id,
            "state": "complete",
            "origin": origin,
            "input": {"name": path.name, "sha256": actual, "bytes": parsed.size, "format": parsed.format},
            "bundle_kind": parsed.bundle_kind,
            "imported_at": imported_at,
            "sessions": session_manifest,
            "warnings": parsed.warnings,
            "result": result,
        }
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as temp_name:
            staging = Path(temp_name)
            staging.chmod(0o700)
            staged: dict[str, Path] = {}
            for candidate in parsed.candidates:
                if candidate.action not in {"new", "revision"}:
                    continue
                needed = list(dict.fromkeys(
                    digest for digest in (candidate.raw_digest, candidate.normalized_digest)
                    if digest not in staged and not (self.objects_dir / _hex_digest(digest)).exists()
                ))
                if not needed:
                    continue
                if candidate.parser_kind == "jsonl":
                    staged.update(self._stage_streamed_candidate(
                        parsed, candidate, staging, needed,
                    ))
                else:
                    raw_data, normalized_data = self._materialize(parsed, candidate)
                    for digest, data in (
                        (candidate.raw_digest, raw_data),
                        (candidate.normalized_digest, normalized_data),
                    ):
                        if digest in needed:
                            staged[digest] = self._write_staged(staging, digest, data)
                    del raw_data, normalized_data
            final_digest, _final_size = _digest_file(parsed.path)
            if final_digest != expected:
                raise LibraryError(
                    "input_changed",
                    "The input changed while content was being staged; nothing was committed.",
                )
            db = self._connect_write()
            try:
                db.execute("BEGIN IMMEDIATE")
                concurrent = db.execute(
                    "SELECT result_json FROM imports WHERE origin=? AND input_digest=? AND state='complete'",
                    (origin, actual),
                ).fetchone()
                if concurrent is not None:
                    db.rollback()
                    prior = json.loads(concurrent["result_json"])
                    prior["idempotent"] = True
                    return prior
                db.execute(
                    "INSERT INTO imports(import_id,origin,input_path,input_name,input_digest,input_format,bundle_kind,state,started_at,completed_at,manifest_json,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (import_id, origin, str(path.resolve()), path.name, actual, parsed.format,
                     parsed.bundle_kind, "applying", imported_at, None,
                     json.dumps(manifest, ensure_ascii=False), json.dumps(result, ensure_ascii=False)),
                )
                for digest, staged_path in list(staged.items()):
                    self._install_object(staged_path, digest)
                for candidate in parsed.candidates:
                    if candidate.action in {"new", "revision"}:
                        sid = assigned[candidate.index]
                        revision_of = candidate.revision_session_id
                        if revision_of is None and candidate.revision_candidate is not None:
                            revision_of = assigned[candidate.revision_candidate]
                        start = candidate.meta.get("start") or ""
                        end = candidate.meta.get("end") or ""
                        mtime = _timestamp_seconds(end) or _timestamp_seconds(start) or _safe_number(candidate.meta.get("mtime")) or time.time()
                        for digest, size, kind in (
                            (candidate.raw_digest, candidate.raw_size, "raw"),
                            (candidate.normalized_digest, candidate.normalized_size, "normalized"),
                        ):
                            db.execute(
                                "INSERT OR IGNORE INTO objects(digest,size,kind,created_at) VALUES(?,?,?,?)",
                                (digest, size, kind, imported_at),
                            )
                        db.execute(
                            "INSERT INTO sessions(session_id,provider,source_format,native_id,title,cwd,project,branch,models_json,efforts_json,tokens_json,diagnostics_json,start_value,end_value,mtime,raw_digest,normalized_digest,raw_object,normalized_object,completeness_json,sensitive_json,primary_origin,import_id,imported_at,revision_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                sid, candidate.provider, candidate.source_format, candidate.native_id,
                                candidate.meta.get("title") or "untitled session",
                                candidate.meta.get("cwd") or "", candidate.meta.get("cwd") or "",
                                candidate.meta.get("branch") or "",
                                json.dumps(candidate.meta.get("models") or []),
                                json.dumps(candidate.meta.get("efforts") or []),
                                json.dumps(candidate.meta.get("tokens") or {}),
                                json.dumps(candidate.meta.get("diagnostics") or {}),
                                str(start), str(end), mtime, candidate.raw_digest,
                                candidate.normalized_digest, candidate.raw_digest,
                                candidate.normalized_digest, json.dumps({
                                    **candidate.completeness,
                                    "bundle_kind": parsed.bundle_kind,
                                    "potentially_incomplete": parsed.bundle_kind == "share",
                                }),
                                json.dumps(candidate.sensitive), origin, import_id, imported_at,
                                revision_of,
                            ),
                        )
                        if revision_of:
                            db.execute(
                                "INSERT OR IGNORE INTO relationships(left_session_id,right_session_id,kind,import_id) VALUES(?,?,?,?)",
                                (sid, revision_of, "revision", import_id),
                            )
                    else:
                        sid = self._resolve_candidate_id(candidate, assigned)
                    if not sid:
                        raise LibraryError("library_error", "An import candidate could not be assigned a stable session ID.")
                    before = db.total_changes
                    db.execute(
                        "INSERT OR IGNORE INTO provenance(provenance_id,session_id,provider,native_id,origin,input_digest,raw_digest,normalized_digest,import_id,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (_new_id("pv_", 10), sid, candidate.provider, candidate.native_id,
                         origin, actual, candidate.raw_digest, candidate.normalized_digest,
                         import_id, imported_at),
                    )
                    if candidate.action == "duplicate" and db.total_changes > before:
                        provenance_added.append(sid)
                    for alias in [candidate.native_id, *candidate.aliases]:
                        if alias:
                            db.execute(
                                "INSERT OR IGNORE INTO aliases(session_id,provider,alias,kind) VALUES(?,?,?,?)",
                                (sid, candidate.provider, alias, "native" if alias == candidate.native_id else "source"),
                            )
                result["provenance_added"] = list(dict.fromkeys(provenance_added))
                manifest["result"] = result
                db.execute(
                    "UPDATE imports SET state='complete',completed_at=?,manifest_json=?,result_json=? WHERE import_id=?",
                    (imported_at, json.dumps(manifest, ensure_ascii=False),
                     json.dumps(result, ensure_ascii=False), import_id),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        import_dir = self.imports_dir / import_id
        import_dir.mkdir(mode=0o700, exist_ok=True)
        manifest_path = import_dir / "manifest.json"
        temporary = import_dir / ".manifest.tmp"
        manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(manifest_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest_path)
        return result

    def get_import(self, import_id: str) -> dict[str, Any]:
        db = self._connect_read()
        if db is None:
            raise LibraryError("import_not_found", "No import has that opaque ID.", {"import_id": import_id})
        try:
            row = db.execute("SELECT * FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise LibraryError("import_not_found", "No import has that opaque ID.", {"import_id": import_id})
            result = json.loads(row["result_json"])
            return {
                "import_id": row["import_id"], "state": row["state"],
                "origin": row["origin"], "input_sha256": row["input_digest"],
                "input_format": row["input_format"], "bundle_kind": row["bundle_kind"],
                "started_at": row["started_at"], "completed_at": row["completed_at"],
                "created": result.get("created", []),
                "duplicates": result.get("duplicates", []),
                "revisions": result.get("revisions", []),
                "provenance_added": result.get("provenance_added", []),
                "conflicts": result.get("conflicts", []),
                "unreadable": result.get("unreadable", []),
                "result": result,
            }
        finally:
            db.close()

    @staticmethod
    def _row_session(row: sqlite3.Row, aliases: list[str] | None = None) -> dict[str, Any]:
        completeness = json.loads(row["completeness_json"])
        return {
            "id": row["session_id"], "session_id": row["session_id"],
            "source": row["provider"], "provider": row["provider"],
            "format": "archive", "source_format": row["source_format"],
            "native_id": row["native_id"] or None, "aliases": aliases or [],
            "title": row["title"], "cwd": row["cwd"], "project": row["project"],
            "branch": row["branch"], "mtime": row["mtime"],
            "size": row["size"] if "size" in row.keys() else 0,
            "origin": row["primary_origin"], "archived": True,
            "import_id": row["import_id"], "imported_at": row["imported_at"],
            "raw_digest": row["raw_digest"], "normalized_digest": row["normalized_digest"],
            "completeness": completeness, "redacted": bool(completeness.get("redacted")),
            "incomplete": bool(completeness.get("potentially_incomplete")),
            "revision_of": row["revision_of"],
        }

    def list_sessions(self, *, origin: str | None = None, source: str | None = None,
                      query: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        db = self._connect_read()
        if db is None:
            return []
        try:
            sql = (
                "SELECT s.*, o.size FROM sessions s JOIN objects o ON o.digest=s.normalized_object "
                "WHERE (? IS NULL OR EXISTS(SELECT 1 FROM provenance p WHERE p.session_id=s.session_id AND p.origin=?)) "
                "AND (? IS NULL OR s.provider=?) ORDER BY s.mtime DESC, s.imported_at DESC LIMIT ?"
            )
            rows = db.execute(sql, (origin, origin, source, source, max(1, min(int(limit), 5000)))).fetchall()
            alias_map: dict[str, list[str]] = {}
            for alias in db.execute("SELECT session_id,alias FROM aliases"):
                alias_map.setdefault(alias["session_id"], []).append(alias["alias"])
            sessions = [self._row_session(row, alias_map.get(row["session_id"], [])) for row in rows]
            if query:
                needle = query.casefold()
                sessions = [
                    session for session in sessions
                    if needle in (
                        " ".join(str(session.get(key) or "") for key in
                                 ("id", "source", "native_id", "title", "cwd", "origin"))
                        + " " + " ".join(session.get("aliases") or [])
                    ).casefold()
                ]
            return sessions
        finally:
            db.close()

    def _resolve(self, value: str) -> sqlite3.Row:
        db = self._connect_read()
        if db is None:
            raise LibraryError("session_not_found", "No session has that identifier.", {"id": value})
        try:
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (value,)).fetchone()
            if row is not None:
                return row
            provider = None
            native = value
            if ":" in value:
                provider, native = value.split(":", 1)
            rows = db.execute(
                "SELECT DISTINCT s.* FROM sessions s LEFT JOIN aliases a ON a.session_id=s.session_id "
                "WHERE (s.native_id=? AND (? IS NULL OR s.provider=?)) "
                "OR (a.alias=? AND (? IS NULL OR a.provider=?)) "
                "ORDER BY s.imported_at DESC",
                (native, provider, provider, native, provider, provider),
            ).fetchall()
            if not rows:
                raise LibraryError("session_not_found", "No session has that identifier.", {"id": value})
            distinct = {row["provider"] for row in rows}
            if provider is None and len(distinct) > 1:
                raise LibraryError("ambiguous_session", "The native identifier matches multiple providers; use provider:native_id or a stable Fables session ID.", {"id": value})
            return rows[0]
        finally:
            db.close()

    def get_session(self, session_id: str) -> dict[str, Any]:
        row = self._resolve(session_id)
        data = self._read_object(row["normalized_object"])
        try:
            archive = json.loads(data)
        except json.JSONDecodeError:
            raise LibraryError("object_corrupt", "The normalized session object failed validation.") from None
        return {"session": self._row_session(row), "archive": archive}

    def get_session_text(self, session_id: str) -> str:
        row = self._resolve(session_id)
        return self._read_object(row["normalized_object"]).decode("utf-8")

    def _read_object(self, digest: str) -> bytes:
        path = self.objects_dir / _hex_digest(digest)
        try:
            data = path.read_bytes()
        except OSError:
            raise LibraryError("object_missing", "A required immutable session object is missing.", {"digest": digest}) from None
        if _digest(data) != digest:
            raise LibraryError("object_corrupt", "An immutable session object failed its SHA-256 check.", {"digest": digest})
        return data

    def provenance(self, session_id: str) -> dict[str, Any]:
        row = self._resolve(session_id)
        sid = row["session_id"]
        db = self._connect_read()
        assert db is not None
        try:
            aliases = [item["alias"] for item in db.execute("SELECT alias FROM aliases WHERE session_id=? ORDER BY alias", (sid,))]
            provenance = [dict(item) for item in db.execute(
                "SELECT provenance_id,provider,native_id,origin,input_digest,raw_digest,normalized_digest,import_id,imported_at FROM provenance WHERE session_id=? ORDER BY imported_at",
                (sid,),
            )]
            revised_by = [item["session_id"] for item in db.execute("SELECT session_id FROM sessions WHERE revision_of=? ORDER BY imported_at", (sid,))]
            attachments = [dict(item) for item in db.execute("SELECT attachment_id,name,media_type,digest,size,available FROM attachments WHERE session_id=?", (sid,))]
            return {
                "session": self._row_session(row, aliases),
                "provenance": provenance,
                "relationships": {"revision_of": row["revision_of"], "revised_by": revised_by},
                "attachments": attachments,
            }
        finally:
            db.close()

    def search(self, query: str, *, origin: str | None = None, source: str | None = None,
               include_tools: bool = False, include_thinking: bool = False,
               limit: int = 20) -> list[dict[str, Any]]:
        needle = str(query or "").strip().casefold()
        if not needle:
            raise LibraryError("query_required", "Search requires a non-empty query.")
        results: list[dict[str, Any]] = []
        sessions = self.list_sessions(origin=origin, source=source, limit=SEARCH_SCAN_LIMIT)
        for session in sessions:
            metadata = " ".join(
                str(session.get(key) or "") for key in
                ("id", "source", "native_id", "title", "cwd", "origin")
            ) + " " + " ".join(session.get("aliases") or [])
            if needle in metadata.casefold():
                results.append({**session, "snippet": "identifier or metadata match"})
                if len(results) >= max(1, min(int(limit), 100)):
                    break
                continue
            archive = self.get_session(session["id"])["archive"]
            for item in archive.get("items") or []:
                kind = item.get("kind")
                if kind not in {"user", "assistant"} and not (include_tools and kind in {"tool", "info"}) and not (include_thinking and kind == "thinking"):
                    continue
                text = " ".join(_strings({key: item.get(key) for key in ("text", "name", "input", "output")}))
                index = text.casefold().find(needle)
                if index < 0:
                    continue
                snippet = " ".join(text[max(0, index - 100):index + len(query) + 180].split())[:320]
                results.append({**session, "snippet": snippet})
                break
            if len(results) >= max(1, min(int(limit), 100)):
                break
        return results
