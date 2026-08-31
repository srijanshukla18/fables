"""Session discovery and loading for Fables.

The functions in this module do not keep global state.  A scan returns opaque
targets for the HTTP layer. Opaque hashes remain the stable request ids;
native provider ids (for example a pi UUID) are also recorded so agents can
look up a session by the id their own tool printed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

PREVIEW_BYTES = 262144

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_GENERIC_NATIVE_IDS = frozenset({
    "session", "sessions", "events", "context", "wire", "transcript",
    "history", "chat", "chats", "thread", "threads", "messages", "data",
    "index", "cli", "agent-transcripts", "projects", "tmp", "chatsessions",
    "conversations", "storage", "message", "part",
})


@dataclass(frozen=True)
class SessionTarget:
    kind: str
    path: Path
    key: str = ""
    extra: tuple[str, ...] = ()


@dataclass
class ProviderResult:
    sessions: list[dict[str, Any]]
    targets: dict[str, SessionTarget]
    status: dict[str, Any]


def _sid(source: str, identity: str) -> str:
    return hashlib.sha1(f"{source}\0{identity}".encode()).hexdigest()[:12]


class AmbiguousSessionId(KeyError):
    """Raised when a native id matches more than one session."""

    def __init__(self, query: str, matches: list[dict[str, Any]]):
        self.query = query
        self.matches = matches
        detail = ", ".join(
            f"{item.get('source')}:{item.get('native_id') or item['id']} "
            f"(id={item['id']})"
            for item in matches
        )
        super().__init__(
            f"Session {query!r} is ambiguous ({len(matches)} matches: {detail}). "
            "Pass source:native_id (for example pi:019ffc61-...) or the opaque "
            "id from list_sessions."
        )


def _looks_like_path(value: str) -> bool:
    if value.startswith(("/", "~", "\\")) or value.startswith("file:"):
        return True
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha() and (
            len(value) == 2 or value[2] in "/\\"):
        return True
    return False


def _clean_native_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or _looks_like_path(text) or text.lower() in _GENERIC_NATIVE_IDS:
        return ""
    return text


def _path_uuid(path: Path) -> str:
    for text in (path.stem, path.parent.name, path.name):
        match = _UUID_RE.search(text)
        if match:
            return match.group(0)
    return ""


def _pick_native(*candidates: Any) -> tuple[str, list[str]]:
    """Return (canonical native id, extra aliases) from provider ids / paths."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, Path):
            value = _path_uuid(candidate)
        else:
            value = _clean_native_id(candidate)
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    if not cleaned:
        return "", []
    uuids = [item for item in cleaned if _UUID_RE.fullmatch(item)]
    canonical = uuids[0] if uuids else cleaned[0]
    aliases = [item for item in cleaned if item.lower() != canonical.lower()]
    return canonical, aliases


def _apply_native(entry: dict[str, Any], *candidates: Any) -> dict[str, Any]:
    canonical, aliases = _pick_native(*candidates)
    if canonical:
        entry["native_id"] = canonical
    if aliases:
        entry["aliases"] = aliases
    return entry


def _native_keys(entry: dict[str, Any]) -> list[str]:
    keys = [str(entry.get("id") or "")]
    native = str(entry.get("native_id") or "")
    if native:
        keys.append(native)
        source = str(entry.get("source") or "")
        if source:
            keys.append(f"{source}:{native}")
    for alias in entry.get("aliases") or ():
        text = str(alias or "")
        if text:
            keys.append(text)
            source = str(entry.get("source") or "")
            if source:
                keys.append(f"{source}:{text}")
    return [item for item in keys if item]


def session_haystack(entry: dict[str, Any]) -> str:
    """Lowercased blob used by list_sessions query matching."""
    parts = [str(entry.get(key) or "") for key in (
        "id", "native_id", "title", "cwd", "project", "source",
    )]
    parts.extend(str(alias) for alias in (entry.get("aliases") or ()) if alias)
    native = str(entry.get("native_id") or "")
    source = str(entry.get("source") or "")
    if native and source:
        parts.append(f"{source}:{native}")
    return " ".join(parts).lower()


def _display_path(value: str | Path, home: Path) -> str:
    text = str(value)
    home_text = str(home)
    if text == home_text:
        return "~"
    if text.startswith(home_text + "/"):
        return "~" + text[len(home_text):]
    return text


def _timestamp(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    if isinstance(value, str) and value:
        try:
            return _timestamp(float(value), default)
        except ValueError:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return default


def _first_lines(path: Path, max_bytes: int = PREVIEW_BYTES) -> Iterable[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(max_bytes)
    except OSError:
        return
    for line in chunk.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(
            str(item.get("text", ""))
            for item in value
            if isinstance(item, dict) and item.get("type", "text") == "text"
        ).strip()
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("content"))
    return ""


def _decode_claude_project(dirname: str, home: Path) -> str:
    parts = [part for part in dirname.split("-") if part]

    def walk(base: Path, index: int) -> Path | None:
        if index == len(parts):
            return base
        segment = parts[index]
        cursor = index
        while cursor < len(parts):
            candidate = base / segment
            if candidate.exists():
                found = walk(candidate, cursor + 1)
                if found is not None:
                    return found
            cursor += 1
            if cursor < len(parts):
                segment += "-" + parts[cursor]
        return None

    resolved = walk(Path("/"), 0)
    display = resolved if resolved else Path("/") / Path(*parts)
    return _display_path(display, home)


def claude_title(path: Path) -> str:
    title = ""
    first_user = ""
    for obj in _first_lines(path):
        kind = obj.get("type")
        if kind == "summary" and obj.get("summary"):
            title = str(obj["summary"])
        elif kind == "ai-title" and obj.get("aiTitle"):
            return str(obj["aiTitle"])
        elif kind == "user" and not first_user and not obj.get("isMeta"):
            content = (obj.get("message") or {}).get("content")
            text = _text(content)
            if text and not text.startswith(("<", "Caveat:")):
                first_user = text
    return title or first_user


def codex_metadata(path: Path) -> tuple[str, str]:
    title = ""
    cwd = ""
    first_user = ""
    for obj in _first_lines(path):
        payload = obj.get("payload") or {}
        if obj.get("type") == "session_meta":
            cwd = str(payload.get("cwd") or "")
        elif obj.get("type") == "event_msg":
            if payload.get("type") == "thread_name_updated" and payload.get("thread_name"):
                return str(payload["thread_name"]), cwd
            if payload.get("type") == "user_message" and not first_user:
                text = _text(payload.get("message"))
                if text and not text.startswith(("<", "#")):
                    first_user = text
    return title or first_user, cwd


def pi_metadata(path: Path) -> tuple[str, str, float, set[str], str]:
    """Return (title, cwd, latest record timestamp, models, session id).

    pi stores one session per JSONL under ~/.pi/agent/sessions/<cwd-slug>/:
    a ``session`` record (cwd + id), ``message`` records with a nested provider
    ``message`` dict, plus ``model_change`` and ``thinking_level_change``
    records. Filenames are ``<timestamp>_<uuid>.jsonl``.
    """
    title = ""
    cwd = ""
    session_id = ""
    latest = 0.0
    models: set[str] = set()
    for obj in _first_lines(path):
        latest = max(latest, _timestamp(obj.get("timestamp")))
        kind = obj.get("type")
        if kind == "session":
            cwd = str(obj.get("cwd") or "")
            session_id = str(obj.get("id") or "")
        elif kind == "model_change" and obj.get("modelId"):
            models.add(str(obj["modelId"]))
        elif kind == "message" and not title:
            message = obj.get("message") or {}
            if message.get("role") == "user" and not obj.get("isMeta"):
                text = _text(message.get("content"))
                if text and not text.startswith(("<", "#")):
                    title = text
    return title, cwd, latest, models, session_id


def scan_pi(home: Path) -> ProviderResult:
    return _scan_pi_like(home, home / ".pi" / "agent" / "sessions", "pi")


def scan_prime(home: Path) -> ProviderResult:
    """Prime Agent sessions.

    Prime Agent (prime-agent) stores sessions under ``~/.prime/agent/sessions/``
    using the same JSONL schema as pi (session/message/model_change records), so
    the shared pi scanner applies unchanged.
    """
    return _scan_pi_like(home, home / ".prime" / "agent" / "sessions", "prime")


def _scan_pi_like(home: Path, root: Path, source: str) -> ProviderResult:
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for path in sorted(root.rglob("*.jsonl")):
            title, cwd, mtime, _models, session_id = pi_metadata(path)
            entry, target = _direct_entry(
                path,
                source,
                source,
                _display_path(cwd, home) if cwd else "",
                title,
                mtime=mtime or None,
                native_id=session_id,
            )
            sessions.append(entry)
            targets[entry["id"]] = target
    return _result(source, sessions, targets)


def _direct_entry(
    path: Path,
    source: str,
    fmt: str,
    project: str,
    title: str,
    *,
    mtime: float | None = None,
    size: int | None = None,
    sub: bool | None = None,
    identity: str | None = None,
    experimental: bool = False,
    native_id: str | None = None,
) -> tuple[dict, SessionTarget]:
    stat = path.stat()
    sid = _sid(source, identity or str(path.resolve()))
    entry: dict[str, Any] = {
        "id": sid,
        "source": source,
        "format": fmt,
        "project": project,
        "title": (title or path.stem)[:120],
        "mtime": stat.st_mtime if mtime is None else mtime,
        "size": stat.st_size if size is None else size,
    }
    if sub is not None:
        entry["sub"] = sub
    if experimental:
        entry["experimental"] = True
    _apply_native(entry, native_id, path)
    return entry, SessionTarget("file", path)


def _result(source: str, sessions: list[dict], targets: dict[str, SessionTarget],
            message: str | None = None, status: str = "ok",
            stability: str | None = None) -> ProviderResult:
    info: dict[str, Any] = {"source": source, "count": len(sessions), "status": status}
    if stability:
        info["stability"] = stability
    if message:
        info["message"] = message
    return ProviderResult(sessions, targets, info)


def scan_claude(home: Path) -> ProviderResult:
    root = home / ".claude" / "projects"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for project_dir in sorted(root.iterdir()):
            if not project_dir.is_dir():
                continue
            project = _decode_claude_project(project_dir.name, home)
            for path in sorted(project_dir.rglob("*.jsonl")):
                entry, target = _direct_entry(
                    path, "claude", "claude", project, claude_title(path),
                    sub="subagents" in path.parts,
                )
                sessions.append(entry)
                targets[entry["id"]] = target
    return _result("claude", sessions, targets)


def scan_codex(home: Path) -> ProviderResult:
    roots = (home / ".codex" / "sessions", home / ".codex" / "archived_sessions")
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            title, cwd = codex_metadata(path)
            entry, target = _direct_entry(
                path, "codex", "codex", _display_path(cwd, home) if cwd else "",
                title,
            )
            sessions.append(entry)
            targets[entry["id"]] = target
    return _result("codex", sessions, targets)


def _find_cowork_metadata(local_dir: Path) -> dict:
    candidates = [local_dir.with_suffix(".json"), local_dir.parent / f"{local_dir.name}.json"]
    for candidate in candidates:
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _first_value(data: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    for container in ("metadata", "session", "workspace"):
        nested = data.get(container)
        if isinstance(nested, dict):
            value = _first_value(nested, keys)
            if value not in (None, ""):
                return value
    return None


def scan_cowork(home: Path) -> ProviderResult:
    root = home / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    local_dirs: set[Path] = set()
    if root.is_dir():
        local_dirs.update(path for path in root.glob("**/local_*") if path.is_dir())
        local_dirs.update(path for path in root.glob("**/agent/local_ditto_*") if path.is_dir())
    for local_dir in sorted(local_dirs):
        project_root = local_dir / ".claude" / "projects"
        if not project_root.is_dir():
            continue
        metadata = _find_cowork_metadata(local_dir)
        metadata_title = _first_value(metadata, ("title", "name", "sessionTitle"))
        metadata_project = _first_value(
            metadata, ("cwd", "project", "projectPath", "workingDirectory")
        )
        metadata_mtime = _timestamp(
            _first_value(metadata, ("lastActivityAt", "updatedAt", "createdAt"))
        )
        for project_dir in sorted(project_root.iterdir()):
            if not project_dir.is_dir():
                continue
            project = (
                _display_path(str(metadata_project), home) if metadata_project
                else _decode_claude_project(project_dir.name, home)
            )
            for path in sorted(project_dir.rglob("*.jsonl")):
                if path.name == "audit.jsonl":
                    continue
                entry, target = _direct_entry(
                    path, "cowork", "claude", project,
                    str(metadata_title or claude_title(path)),
                    mtime=metadata_mtime or None,
                    sub="subagents" in path.parts,
                    experimental=True,
                )
                sessions.append(entry)
                targets[entry["id"]] = target
    return _result(
        "cowork", sessions, targets,
        "local legacy sandbox format; current account-backed Cowork may not appear.",
        stability="experimental",
    )


def parse_simple_yaml(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        if not line or line[0].isspace() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key, raw = key.strip(), raw.strip()
        if not key:
            continue
        if raw.startswith(("'", '"')) and raw.endswith(raw[:1]):
            try:
                result[key] = json.loads(raw) if raw.startswith('"') else raw[1:-1].replace("''", "'")
            except json.JSONDecodeError:
                result[key] = raw[1:-1]
        elif raw.lower() in ("true", "false"):
            result[key] = raw.lower() == "true"
        elif raw.lower() in ("null", "~"):
            result[key] = None
        else:
            try:
                result[key] = int(raw)
            except ValueError:
                result[key] = raw.split(" #", 1)[0].strip()
    return result


def copilot_title(path: Path) -> str:
    for obj in _first_lines(path):
        if obj.get("type") == "user.message":
            data = obj.get("data") or {}
            text = _text(data.get("content") if isinstance(data, dict) else None)
            if not text:
                text = _text(obj.get("content"))
            if text:
                return text
    return ""


def scan_copilot(home: Path) -> ProviderResult:
    root = home / ".copilot" / "session-state"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for path in sorted(root.glob("*/events.jsonl")):
            workspace = parse_simple_yaml(path.parent / "workspace.yaml")
            title = str(workspace.get("name") or copilot_title(path))
            project = _display_path(str(workspace.get("cwd") or ""), home)
            mtime = _timestamp(workspace.get("updated_at"), path.stat().st_mtime)
            entry, target = _direct_entry(
                path, "copilot", "copilot", project, title,
                mtime=mtime, identity=str(path.resolve()),
                native_id=str(workspace.get("id") or path.parent.name),
            )
            sessions.append(entry)
            targets[entry["id"]] = target
    return _result("copilot", sessions, targets)


def scan_cursor_cli(home: Path) -> ProviderResult:
    """Cursor CLI agent transcripts.

    The Cursor CLI agent stores one JSONL transcript per session under
    ``<root>/<project-slug>/agent-transcripts/<session-id>/`` where root is
    ``~/.cursor/projects``, ``~/.agent``, or ``~/.agents``. Records are
    ``{"role": ..., "message": {"content": [...]}}`` with text and
    ``tool_use`` blocks.
    """
    roots = (home / ".cursor" / "projects", home / ".agent", home / ".agents")
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/agent-transcripts/*/*.jsonl")):
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            slug = path.parent.parent.parent.name
            project = _decode_claude_project(slug, home)
            entry, target = _direct_entry(
                path, "cursor-cli", "cursor-cli", project,
                _role_first_title(path),
                native_id=path.parent.name,
            )
            sessions.append(entry)
            targets[entry["id"]] = target
    return _result("cursor-cli", sessions, targets)


def _role_first_title(path: Path) -> str:
    """First user question from a role-based transcript (Cursor CLI)."""
    for obj in _first_lines(path):
        message = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(message, dict) or obj.get("role") != "user":
            continue
        text = _text(message.get("content"))
        if not text:
            continue
        match = re.search(r"<user_query>([\s\S]*?)</user_query>", text)
        if match:
            return match.group(1).strip()[:120]
        if not text.startswith(("<", "#")):
            return text[:120]
    return ""


def scan_kimi(home: Path) -> ProviderResult:
    """Kimi CLI sessions.

    Each conversation lives in ``~/.kimi/sessions/<user-id>/<conv-id>/`` with
    a compact ``context.jsonl`` message log and a ``wire.jsonl`` event log
    that carries tool-call names and arguments.
    """
    root = home / ".kimi" / "sessions"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for user_dir in sorted(root.iterdir()):
            if not user_dir.is_dir():
                continue
            for conv_dir in sorted(user_dir.iterdir()):
                context = conv_dir / "context.jsonl"
                if not context.is_file():
                    continue
                stat = context.stat()
                sid = _sid("kimi", str(context.resolve()))
                entry: dict[str, Any] = {
                    "id": sid,
                    "source": "kimi",
                    "format": "kimi",
                    "project": "",
                    "title": _kimi_title(context),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }
                _apply_native(entry, conv_dir.name)
                wire = conv_dir / "wire.jsonl"
                targets[sid] = SessionTarget(
                    "kimi", context, "", (str(wire),) if wire.is_file() else (),
                )
                sessions.append(entry)
    return _result("kimi", sessions, targets)


def _kimi_title(context: Path) -> str:
    for obj in _first_lines(context):
        if isinstance(obj, dict) and obj.get("role") == "user":
            text = _text(obj.get("content"))
            if text and not text.startswith(("<", "#")):
                return text[:120]
    return ""


def _commandcode_project(dirname: str, home: Path) -> str:
    """Decode a Command Code project slug like ``users-srijanshukla-code``."""
    if dirname.startswith("users-"):
        text = "Users/" + dirname[len("users-"):].replace("-", "/")
        return _display_path(Path("/") / text, home)
    return dirname


def _commandcode_title(path: Path) -> str:
    for obj in _first_lines(path):
        if isinstance(obj, dict) and obj.get("role") == "user":
            text = _text(obj.get("content"))
            if text and not text.startswith(("<", "#")):
                return text[:120]
    return ""


def scan_commandcode(home: Path) -> ProviderResult:
    """Command Code CLI sessions.

    Each session is ``~/.commandcode/projects/<slug>/<uuid>.jsonl`` with
    role-based records (``content`` blocks: text, reasoning, tool_use) plus a
    sibling ``<uuid>.meta.json`` carrying the title and model.
    """
    root = home / ".commandcode" / "projects"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for project_dir in sorted(root.iterdir()):
            if not project_dir.is_dir():
                continue
            project = _commandcode_project(project_dir.name, home)
            for path in sorted(project_dir.glob("*.jsonl")):
                if path.name.endswith(".checkpoints.jsonl"):
                    continue
                meta: dict[str, Any] = {}
                meta_path = path.with_name(path.stem + ".meta.json")
                if meta_path.is_file():
                    meta = _read_json(meta_path) or {}
                entry, target = _direct_entry(
                    path, "commandcode", "commandcode", project,
                    str(meta.get("title") or "") or _commandcode_title(path),
                    identity=str(path.resolve()),
                    native_id=path.stem,
                )
                if meta.get("model"):
                    entry["model"] = str(meta["model"])
                sessions.append(entry)
                targets[entry["id"]] = target
    return _result("commandcode", sessions, targets)


def scan_amp(home: Path) -> ProviderResult:
    """Amp (ampcode) conversation threads.

    Threads are primarily server-side; the CLI caches them locally as
    ``~/.local/share/amp/threads/<thread-id>.json`` with ``messages`` as a
    JSON string.
    """
    root = home / ".local" / "share" / "amp" / "threads"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            data = _read_json(path)
            if not data:
                continue
            sid = _sid("amp", str(path.resolve()))
            title = ""
            stamp = _timestamp(data.get("created"), path.stat().st_mtime)
            try:
                messages = json.loads(str(data.get("messages") or "[]"))
            except json.JSONDecodeError:
                messages = []
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        text = _text(message.get("content") or message.get("text"))
                        if text and not text.startswith(("<", "#")):
                            title = text[:120]
                            break
            entry: dict[str, Any] = {
                "id": sid, "source": "amp", "format": "amp",
                "project": "", "title": title or str(data.get("id") or path.stem)[:120],
                "mtime": stamp, "size": path.stat().st_size,
                "experimental": True,
            }
            _apply_native(entry, data.get("id"), path.stem)
            sessions.append(entry)
            targets[sid] = SessionTarget("amp", path)
    return _result("amp", sessions, targets,
                   stability="experimental")


def _load_amp(target: SessionTarget) -> str:
    data = _read_json(target.path) or {}
    try:
        messages = json.loads(str(data.get("messages") or "[]"))
    except json.JSONDecodeError:
        messages = []
    normalized: list[dict] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant", "tool", "system"):
                continue
            content = message.get("content") or message.get("text") or ""
            if isinstance(content, list):
                blocks = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") in ("text", "tool_use", "tool_result",
                                                  "reasoning", "thinking"):
                            blocks.append(block)
                content = blocks
            normalized.append({"message": {"role": role, "content": content}})
    return json.dumps({"ampArchive": True, "session": {}, "messages": normalized},
                      separators=(",", ":"), ensure_ascii=False)


def scan_qwen(home: Path) -> ProviderResult:
    """Qwen Code sessions: ``~/.qwen/tmp/<project>/chats/<uuid>.jsonl``."""
    root = home / ".qwen" / "tmp"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for path in sorted(root.glob("*/chats/*.jsonl")):
            title = _qwen_title(path)
            entry, target = _direct_entry(
                path, "qwen", "qwen", "", title, identity=str(path.resolve()),
                native_id=path.stem,
            )
            sessions.append(entry)
            targets[entry["id"]] = target
    return _result("qwen", sessions, targets)


def _qwen_title(path: Path) -> str:
    for obj in _first_lines(path):
        if isinstance(obj, dict) and obj.get("role") == "user":
            text = _text(obj.get("content"))
            if text and not text.startswith(("<", "#")):
                return text[:120]
    return ""


AIDER_ROOTS = ("code", "projects", "src", "work", "repos")


def scan_aider(home: Path) -> ProviderResult:
    """Aider histories: ``.aider.chat.history.md`` in each repo.

    Aider stores no global session store; histories live next to the code.
    Fables scans the common project roots (one level deep) for the marker
    file.
    """
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    seen: set[str] = set()
    for subdir in AIDER_ROOTS:
        root = home / subdir
        if not root.is_dir():
            continue
        for project_dir in sorted(root.iterdir()):
            if not project_dir.is_dir():
                continue
            path = project_dir / ".aider.chat.history.md"
            if not path.is_file():
                continue
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            lines = [line for line in path.read_text(encoding="utf-8",
                                                     errors="replace").splitlines()
                     if line.strip()][:200]
            title = ""
            for line in lines:
                if line.startswith("# ") and not line.startswith("#### "):
                    title = line[2:].strip()
                    break
            entry, target = _direct_entry(
                path, "aider", "aider", _display_path(project_dir, home),
                title or "Aider session", identity=resolved,
            )
            sessions.append(entry)
            targets[entry["id"]] = target
    return _result("aider", sessions, targets)


def scan_trae(home: Path) -> ProviderResult:
    """Trae IDE conversations (experimental).

    Trae is a VS Code fork; its chats live in workspace chatSessions files
    and, when present, in the ``chat.ChatSessionStore`` index inside
    ``state.vscdb``. The vscdb schema is undocumented, so index-backed
    sessions are best-effort.
    """
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    app_support = home / "Library" / "Application Support" / "Trae"
    user = app_support / "User"
    workspace = user / "workspaceStorage"
    if workspace.is_dir():
        for path in sorted(workspace.glob("*/chatSessions/*.jsonl")):
            entry, target = _direct_entry(
                path, "trae", "trae", "", "", identity=str(path.resolve()),
                native_id=path.stem,
            )
            entry["title"] = _role_first_title(path) or "Trae conversation"
            sessions.append(entry)
            targets[entry["id"]] = target
    vscdb = user / "globalStorage" / "state.vscdb"
    if vscdb.is_file():
        try:
            with _sqlite_ro(vscdb) as connection:
                row = connection.execute(
                    "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
                ).fetchone()
            if row:
                data = _json_value(row["value"])
                entries = (data or {}).get("entries") or {}
                for session_id, _meta in sorted(entries.items()):
                    sid = _sid("trae", f"{vscdb.resolve()}:{session_id}")
                    title = ""
                    if isinstance(_meta, dict):
                        title = str(_meta.get("title") or "")
                    sessions.append(_apply_native({
                        "id": sid, "source": "trae", "format": "trae",
                        "project": "", "title": (title or "Trae conversation")[:120],
                        "mtime": vscdb.stat().st_mtime,
                        "size": vscdb.stat().st_size, "experimental": True,
                    }, session_id))
                    targets[sid] = SessionTarget(
                        "trae", vscdb, session_id, ("chat.ChatSessionStore.",),
                    )
        except (sqlite3.Error, OSError):
            pass
    return _result("trae", sessions, targets, stability="experimental")


def _load_trae(target: SessionTarget) -> str:
    with _sqlite_ro(target.path) as connection:
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            (f"{target.extra[0]}{target.key}",),
        ).fetchone()
    if row is None:
        raise FileNotFoundError("Trae session is unavailable")
    return json.dumps({"traeArchive": True, "session": {}, "messages": []},
                      separators=(",", ":"))


def scan_kiro(home: Path) -> ProviderResult:
    """Kiro CLI ACP sessions: ``~/.kiro/sessions/cli/<id>.jsonl``."""
    root = home / ".kiro" / "sessions" / "cli"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        for path in sorted(root.glob("*.jsonl")):
            entry, target = _direct_entry(
                path, "kiro", "kiro", "", "", identity=str(path.resolve()),
                native_id=path.stem,
            )
            entry["title"] = _kiro_title(path) or "Kiro session"
            sessions.append(entry)
            targets[entry["id"]] = target
    return _result("kiro", sessions, targets)


def _kiro_title(path: Path) -> str:
    for obj in _first_lines(path):
        if not isinstance(obj, dict):
            continue
        text = _text(obj.get("content") or obj.get("message") or obj.get("text"))
        if text and not text.startswith(("<", "#")):
            return text[:120]
    return ""


def scan_zed(home: Path) -> ProviderResult:
    """Zed agent threads: ``threads.db`` SQLite with zstd-compressed blobs."""
    candidates = (
        home / "Library" / "Application Support" / "Zed" / "threads" / "threads.db",
        home / ".local" / "share" / "zed" / "threads" / "threads.db",
    )
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    # Legacy assistant-panel conversations (plain JSON), when present.
    for conv_root in (home / ".config" / "zed" / "conversations",
                      home / "Library" / "Application Support" / "Zed" /
                      "conversations"):
        if not conv_root.is_dir():
            continue
        for path in sorted(conv_root.glob("*.json")):
            entry, target = _direct_entry(
                path, "zed", "zed", "", "Zed conversation",
                identity=str(path.resolve()),
                native_id=path.stem,
            )
            entry["experimental"] = True
            sessions.append(entry)
            targets[entry["id"]] = target
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with _sqlite_ro(path) as connection:
                rows = connection.execute(
                    "SELECT id, data, data_type FROM threads"
                ).fetchall()
        except (sqlite3.Error, OSError):
            continue
        for row in rows:
            thread_id = str(row["id"])
            sid = _sid("zed", f"{path.resolve()}:{thread_id}")
            title = ""
            size = len(row["data"] or b"")
            if _zed_decompress(row["data"], row["data_type"]):
                title = "Zed thread"
            sessions.append(_apply_native({
                "id": sid, "source": "zed", "format": "zed",
                "project": "", "title": title or "Zed thread",
                "mtime": path.stat().st_mtime, "size": size,
                "experimental": True,
            }, thread_id))
            targets[sid] = SessionTarget("zed", path, thread_id, (row["data_type"],))
        break
    return _result("zed", sessions, targets, stability="experimental")


def _zed_decompress(data: bytes, data_type: Any) -> str | None:
    if not data:
        return None
    try:
        if data_type == "zstd":
            try:
                import zlib
                decompressed = zlib.zstd_decompress(data)  # Python 3.14+
            except AttributeError:
                return None
        else:
            decompressed = data
        value = json.loads(decompressed.decode("utf-8", "replace"))
        return json.dumps(value, ensure_ascii=False)
    except (ValueError, TypeError):
        return None


def _load_zed(target: SessionTarget) -> str:
    with _sqlite_ro(target.path) as connection:
        row = connection.execute(
            "SELECT data, data_type FROM threads WHERE id = ?", (target.key,),
        ).fetchone()
    if row is None:
        raise FileNotFoundError("Zed thread is unavailable")
    payload = _zed_decompress(row["data"], row["data_type"])
    if payload is None:
        return json.dumps({"zedArchive": True, "session": {}, "messages": []},
                          separators=(",", ":"))
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {}
    messages: list[dict] = []
    for item in (data.get("messages") or data.get("turns") or []):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ("user", "assistant", "tool", "system"):
            continue
        messages.append({"message": {"role": role, "content": item.get("content")}})
    return json.dumps({"zedArchive": True, "session": {}, "messages": messages},
                      separators=(",", ":"), ensure_ascii=False)


def _extension_task_roots(home: Path, source: str) -> list[Path]:
    if source == "cline":
        roots = [home / ".cline" / "data" / "tasks"]
        extension = "saoudrizwan.claude-dev"
    elif source == "kilo":
        roots = []
        extension = "kilocode.kilo-code"
    else:
        roots = []
        extension = "rooveterinaryinc.roo-cline"
    app_support = home / "Library" / "Application Support"
    for editor in ("Code", "Code - Insiders", "Cursor", "Windsurf"):
        candidate = app_support / editor / "User" / "globalStorage" / extension / "tasks"
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def _task_metadata(task_dir: Path, source: str) -> tuple[dict, str]:
    names = (
        ("task_metadata.json", "history_item.json")
        if source in ("cline", "kilo") else
        ("history_item.json", "task_metadata.json")
    )
    for name in names:
        value = _read_json(task_dir / name)
        if value is not None:
            return value, name
    return {}, names[0]


def _task_files(task_dir: Path, metadata_name: str) -> list[Path]:
    names = {metadata_name, "api_conversation_history.json", "ui_messages.json"}
    return [task_dir / name for name in sorted(names) if (task_dir / name).is_file()]


def _task_usage(metadata: dict) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for key in ("totalCost", "cost", "total_cost"):
        if metadata.get(key) is not None:
            usage["cost"] = metadata[key]
            break
    for key in ("tokensIn", "tokensOut", "inputTokens", "outputTokens",
                "cacheWrites", "cacheReads", "totalTokens"):
        if metadata.get(key) is not None:
            usage[key] = metadata[key]
    nested = metadata.get("tokens")
    if isinstance(nested, (dict, int, float)):
        usage["tokens"] = nested
    return usage


def _scan_extension_tasks(home: Path, source: str) -> ProviderResult:
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    seen: set[str] = set()
    for root in _extension_task_roots(home, source):
        if not root.is_dir():
            continue
        for task_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            metadata, metadata_name = _task_metadata(task_dir, source)
            task_id = str(
                metadata.get("taskId") or metadata.get("id") or
                metadata.get("task_id") or task_dir.name
            )
            if task_id in seen:
                continue
            files = _task_files(task_dir, metadata_name)
            if not files:
                continue
            seen.add(task_id)
            title = _first_value(
                metadata, ("title", "task", "name", "customTitle", "description")
            )
            workspace = _first_value(
                metadata, ("workspaceDirectory", "cwd", "workspacePath",
                           "projectPath", "rootPath", "path")
            )
            if not workspace and isinstance(metadata.get("workspace"), str):
                workspace = metadata["workspace"]
            if isinstance(workspace, dict):
                workspace = (
                    workspace.get("path") or workspace.get("cwd") or
                    workspace.get("workspaceDirectory")
                )
            fallback_mtime = max(path.stat().st_mtime for path in files)
            mtime = _timestamp(
                _first_value(
                    metadata,
                    ("updatedAt", "lastUpdated", "lastActivityAt", "createdAt", "ts"),
                ),
                fallback_mtime,
            )
            size = sum(path.stat().st_size for path in files)
            sid = _sid(source, task_id)
            entry: dict[str, Any] = {
                "id": sid, "source": source, "format": source,
                "project": _display_path(str(workspace or ""), home),
                "title": str(title or task_id)[:120],
                "mtime": mtime, "size": size,
            }
            _apply_native(entry, task_id, task_dir.name)
            usage = _task_usage(metadata)
            if usage:
                entry["usage"] = usage
            sessions.append(entry)
            targets[sid] = SessionTarget(
                "extension-task", task_dir, task_id, (metadata_name,)
            )
    return _result(source, sessions, targets)


def scan_cline(home: Path) -> ProviderResult:
    return _scan_extension_tasks(home, "cline")


def scan_roo(home: Path) -> ProviderResult:
    return _scan_extension_tasks(home, "roo")


def _gemini_project(path: Path, data: dict, home: Path) -> str:
    chats_dir = next((parent for parent in path.parents if parent.name == "chats"), path.parent)
    marker = chats_dir.parent / ".project_root"
    try:
        project = marker.read_text(encoding="utf-8").strip()
    except OSError:
        project = ""
    if project:
        return _display_path(project, home)
    directories = data.get("directories")
    if isinstance(directories, list) and directories:
        return _display_path(str(directories[0]), home)
    project_hash = str(data.get("projectHash") or chats_dir.parent.name)
    return f"Gemini project {project_hash[:16]}"


def _gemini_jsonl(path: Path) -> dict:
    metadata: dict[str, Any] = {}
    messages: list[dict] = []
    positions: dict[str, int] = {}
    checkpoints: list[dict] = []
    for record in _first_lines(path, max_bytes=max(path.stat().st_size, PREVIEW_BYTES)):
        update = record.get("$set")
        if isinstance(update, dict):
            metadata.update(update)
            continue
        rewind = record.get("$rewindTo")
        if rewind is not None:
            marker = positions.get(str(rewind))
            if marker is not None:
                messages = messages[:marker + 1]
                positions = {
                    str(item.get("id")): index
                    for index, item in enumerate(messages) if item.get("id") is not None
                }
            checkpoints.append(record)
            continue
        if "$checkpoint" in record or record.get("type") == "checkpoint":
            checkpoints.append(record)
            continue
        value = record.get("message") if isinstance(record.get("message"), dict) else record
        message_id = value.get("id")
        if message_id is not None and value.get("type") in {
            "user", "gemini", "info", "error", "warning"
        }:
            key = str(message_id)
            if key in positions:
                messages[positions[key]] = value
            else:
                positions[key] = len(messages)
                messages.append(value)
            continue
        if record.get("sessionId") or record.get("projectHash"):
            metadata.update(record)
    metadata.pop("messages", None)
    archive = dict(metadata)
    archive["messages"] = messages
    if checkpoints:
        archive["checkpoints"] = checkpoints
    return archive


def scan_gemini(home: Path) -> ProviderResult:
    root = home / ".gemini" / "tmp"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if root.is_dir():
        direct = list(root.glob("*/chats/session-*.json"))
        direct.extend(root.glob("*/chats/session-*.jsonl"))
        nested = list(root.glob("*/chats/*/*.jsonl"))
        for path in sorted({*direct, *nested}):
            is_jsonl = path.suffix == ".jsonl"
            data = _gemini_jsonl(path) if is_jsonl else _read_json(path)
            if not data:
                continue
            first_user = ""
            for message in data.get("messages") or []:
                if isinstance(message, dict) and message.get("type") == "user":
                    first_user = _text(
                        message.get("content") or message.get("displayContent")
                    )
                    if first_user:
                        break
            stat = path.stat()
            entry, target = _direct_entry(
                path, "gemini", "gemini", _gemini_project(path, data, home),
                first_user or str(data.get("sessionId") or path.stem),
                mtime=_timestamp(data.get("lastUpdated"), stat.st_mtime),
                identity=str(path.resolve()),
                sub=path in nested,
                native_id=str(data.get("sessionId") or path.stem),
            )
            sessions.append(entry)
            targets[entry["id"]] = (
                SessionTarget("gemini-jsonl", path) if is_jsonl else target
            )
    return _result("gemini", sessions, targets)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _opencode_time(data: dict, default: float) -> float:
    times = data.get("time")
    if isinstance(times, dict):
        return _timestamp(times.get("updated") or times.get("created"), default)
    return _timestamp(data.get("time_updated") or data.get("updatedAt"), default)


def _scan_opencode_legacy(root: Path, home: Path) -> tuple[list[dict], dict[str, SessionTarget]]:
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    session_root = root / "session"
    if not session_root.is_dir():
        return sessions, targets
    for path in sorted(session_root.rglob("*.json")):
        data = _read_json(path)
        if not data or not data.get("id"):
            continue
        session_id = str(data["id"])
        stat = path.stat()
        project = data.get("directory") or data.get("projectID") or ""
        entry, _ = _direct_entry(
            path, "opencode", "opencode", _display_path(str(project), home),
            str(data.get("title") or session_id),
            mtime=_opencode_time(data, stat.st_mtime),
            identity=f"legacy:{session_id}:{path.resolve()}",
            experimental=True,
            native_id=session_id,
        )
        target = SessionTarget("opencode-legacy", path, session_id, (str(root),))
        sessions.append(entry)
        targets[entry["id"]] = target
    return sessions, targets


def _sqlite_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=200")
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _selected_columns(available: set[str], wanted: tuple[str, ...]) -> list[str]:
    return [column for column in wanted if column in available]


def _decode_json_columns(row: sqlite3.Row) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if value is not None and key.endswith("_json"):
            try:
                value = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                pass
        if value is not None:
            result[key] = value
    return result


GOOSE_SESSION_FIELDS = (
    "id", "session_id", "name", "working_dir", "created_at", "updated_at",
    "session_type", "provider_name", "model_config_json", "input_tokens",
    "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens",
    "cached_tokens", "total_cost", "cost", "project_id", "parent_id",
    "parent_session_id", "total_input_tokens", "total_output_tokens",
    "total_cached_tokens", "total_cache_read_tokens", "total_cache_write_tokens",
    "total_cache_creation_tokens",
)
GOOSE_MESSAGE_FIELDS = (
    "message_id", "id", "session_id", "role", "content_json",
    "created_timestamp", "created_at", "tokens", "metadata_json",
)


def _goose_table(connection: sqlite3.Connection, plural: str) -> str | None:
    tables = _tables(connection)
    if plural in tables:
        return plural
    singular = plural.removesuffix("s")
    return singular if singular in tables else None


def _scan_goose_db(
    path: Path, home: Path, seen: set[str]
) -> tuple[list[dict], dict[str, SessionTarget]]:
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    with _sqlite_ro(path) as connection:
        table = _goose_table(connection, "sessions")
        if table is None:
            raise sqlite3.DatabaseError("sessions table is missing")
        columns = _table_columns(connection, table)
        id_column = "id" if "id" in columns else "session_id" if "session_id" in columns else None
        if id_column is None:
            raise sqlite3.DatabaseError("sessions table has no session id")
        selected = _selected_columns(columns, GOOSE_SESSION_FIELDS)
        query = ", ".join(f'"{column}"' for column in selected)
        for row in connection.execute(f'SELECT {query} FROM "{table}"'):
            data = _decode_json_columns(row)
            session_id = str(data.get(id_column) or "")
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            mtime = _timestamp(
                data.get("updated_at") or data.get("created_at"),
                path.stat().st_mtime,
            )
            sid = _sid("goose", f"{path.resolve()}:{session_id}")
            entry: dict[str, Any] = {
                "id": sid, "source": "goose", "format": "goose",
                "project": _display_path(str(data.get("working_dir") or ""), home),
                "title": str(data.get("name") or session_id)[:120],
                "mtime": mtime,
                "size": len(json.dumps(data, ensure_ascii=False, default=str)),
            }
            _apply_native(entry, session_id)
            if data.get("provider_name") is not None:
                entry["provider"] = data["provider_name"]
            model_config = data.get("model_config_json")
            if isinstance(model_config, dict):
                model = model_config.get("model") or model_config.get("model_name")
                if model:
                    entry["model"] = model
            sessions.append(entry)
            targets[sid] = SessionTarget("goose", path, session_id, (table, id_column))
    return sessions, targets


def scan_goose(home: Path) -> ProviderResult:
    paths = (
        home / "Library" / "Application Support" / "Block" / "goose" /
        "sessions" / "sessions.db",
        home / ".local" / "share" / "goose" / "sessions" / "sessions.db",
    )
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    seen: set[str] = set()
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            found, found_targets = _scan_goose_db(path, home, seen)
            sessions.extend(found)
            targets.update(found_targets)
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        return _result(
            "goose", sessions, targets,
            "Goose database unavailable: " + "; ".join(errors), "warning",
        )
    return _result("goose", sessions, targets)


def _row_object(row: sqlite3.Row) -> dict:
    value: dict[str, Any] = {}
    if "data" in row.keys() and row["data"]:
        try:
            parsed = json.loads(row["data"])
            if isinstance(parsed, dict):
                value.update(parsed)
        except (TypeError, json.JSONDecodeError):
            pass
    for key in row.keys():
        if key != "data" and row[key] is not None:
            value.setdefault(key, row[key])
    return value


def _scan_opencode_db(path: Path, home: Path) -> tuple[list[dict], dict[str, SessionTarget]]:
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    with _sqlite_ro(path) as connection:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "session" not in tables:
            return sessions, targets
        columns = _table_columns(connection, "session")
        id_column = "id" if "id" in columns else None
        if not id_column:
            return sessions, targets
        for row in connection.execute('SELECT * FROM "session"'):
            data = _row_object(row)
            session_id = str(data.get("id") or "")
            if not session_id:
                continue
            project = data.get("directory") or data.get("project_id") or data.get("projectID") or ""
            title = str(data.get("title") or session_id)
            times = data.get("time") if isinstance(data.get("time"), dict) else {}
            mtime = _timestamp(
                data.get("time_updated") or data.get("updated_at") or
                times.get("updated"),
                path.stat().st_mtime,
            )
            sid = _sid("opencode", f"sqlite:{path.resolve()}:{session_id}")
            sessions.append(_apply_native({
                "id": sid, "source": "opencode", "format": "opencode",
                "project": _display_path(str(project), home), "title": title[:120],
                "mtime": mtime, "size": len(json.dumps(data, default=str)),
                "experimental": True,
            }, session_id))
            targets[sid] = SessionTarget("opencode-sqlite", path, session_id)
    return sessions, targets


def scan_opencode(home: Path) -> ProviderResult:
    base = home / ".local" / "share" / "opencode"
    sessions, targets = _scan_opencode_legacy(base / "storage", home)
    db = base / "opencode.db"
    message = None
    status = "ok"
    if db.is_file():
        try:
            db_sessions, db_targets = _scan_opencode_db(db, home)
            db_keys = {target.key for target in db_targets.values()}
            if db_keys:
                legacy_ids = {
                    sid for sid, target in targets.items() if target.key in db_keys
                }
                sessions = [
                    session for session in sessions if session["id"] not in legacy_ids
                ]
                for sid in legacy_ids:
                    targets.pop(sid, None)
            sessions.extend(db_sessions)
            targets.update(db_targets)
        except sqlite3.Error as exc:
            status, message = "warning", f"opencode.db unavailable: {exc}"
    if status == "ok":
        message = "Local OpenCode storage is undocumented and migrating."
    return _result(
        "opencode", sessions, targets, message, status,
        stability="experimental",
    )


CURSOR_COMPOSER_FIELDS = (
    "composerId", "name", "createdAt", "lastUpdatedAt", "model", "modelId",
    "mode", "workspaceProjectDir", "workspace", "fullConversationHeadersOnly",
)
CURSOR_BUBBLE_FIELDS = (
    "bubbleId", "type", "text", "richText", "createdAt", "timingInfo",
    "tokenCount", "modelInfo", "isThought", "thinking", "thinkingDurationMs",
    "toolFormerData", "toolResults", "capabilityType", "codeBlocks",
    "intermediateChunks", "errorDetails",
)


def _json_value(value: Any) -> dict | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _cursor_selected(value: dict, fields: tuple[str, ...]) -> dict:
    return {field: value[field] for field in fields if field in value}


def _cursor_project(composer: dict, home: Path) -> str:
    direct = composer.get("workspaceProjectDir")
    if direct:
        return _display_path(str(direct), home)
    workspace = composer.get("workspace")
    if isinstance(workspace, dict):
        for key in ("projectDir", "workspaceFolder", "path", "rootPath"):
            if workspace.get(key):
                return _display_path(str(workspace[key]), home)
    return "Cursor"


def _cursor_store(connection: sqlite3.Connection) -> str:
    tables = {row["name"] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "cursorDiskKV" in tables:
        return "cursorDiskKV"
    if "ItemTable" in tables:
        return "ItemTable"
    raise sqlite3.DatabaseError("no Cursor key/value table")


def _cursor_get(connection: sqlite3.Connection, table: str, key: str) -> dict | None:
    row = connection.execute(
        f'SELECT value FROM "{table}" WHERE key = ?', (key,)
    ).fetchone()
    return _json_value(row["value"]) if row else None


def _cursor_conversation(composer: dict) -> tuple[list[dict], bool]:
    headers = composer.get("fullConversationHeadersOnly")
    if isinstance(headers, list) and headers:
        return [item for item in headers if isinstance(item, dict)], False
    conversation = composer.get("conversation")
    if isinstance(conversation, list):
        return [item for item in conversation if isinstance(item, dict)], True
    return [], False


def scan_cursor(home: Path) -> ProviderResult:
    path = home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    if not path.is_file():
        return _result("cursor", sessions, targets)
    try:
        with _sqlite_ro(path) as connection:
            table = _cursor_store(connection)
            rows = connection.execute(
                f'SELECT key, value FROM "{table}" WHERE key LIKE ?', ("composerData:%",)
            )
            for row in rows:
                composer = _json_value(row["value"])
                if not composer:
                    continue
                composer_id = str(composer.get("composerId") or str(row["key"]).split(":", 1)[-1])
                headers, inline = _cursor_conversation(composer)
                if not headers:
                    continue
                title = str(composer.get("name") or "").strip()
                if not title:
                    for header in headers:
                        if header.get("type") != 1:
                            continue
                        bubble = header if inline else _cursor_get(
                            connection, table,
                            f"bubbleId:{composer_id}:{header.get('bubbleId')}"
                        )
                        title = _text((bubble or {}).get("text"))
                        if title:
                            break
                sid = _sid("cursor", f"{path.resolve()}:{composer_id}")
                stamp = _timestamp(
                    composer.get("lastUpdatedAt") or composer.get("createdAt"),
                    path.stat().st_mtime,
                )
                sessions.append(_apply_native({
                    "id": sid, "source": "cursor", "format": "cursor",
                    "project": _cursor_project(composer, home),
                    "title": (title or "Cursor conversation")[:120],
                    "mtime": stamp, "size": len(row["value"]),
                    "experimental": True,
                }, composer_id))
                targets[sid] = SessionTarget("cursor", path, composer_id, (table,))
    except (sqlite3.Error, OSError) as exc:
        return _result(
            "cursor", [], {}, f"Cursor database unavailable: {exc}", "warning",
            stability="experimental",
        )
    return _result(
        "cursor", sessions, targets,
        "Cursor's local database schema is undocumented and migrating.",
        stability="experimental",
    )


def _mutation_container(state: Any, path: list[Any]) -> Any:
    value = state
    for key in path:
        if isinstance(value, list):
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value[key]
        else:
            raise ValueError("mutation path does not address an object")
    return value


def _vscode_replay(path: Path) -> tuple[dict, dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    raw_lines = text.splitlines()
    operations: list[dict] = []
    torn = 0
    last_content = max((index for index, line in enumerate(raw_lines) if line.strip()), default=-1)
    for index, line in enumerate(raw_lines):
        if not line.strip():
            continue
        try:
            operation = json.loads(line)
        except json.JSONDecodeError:
            if index == last_content:
                torn = 1
                continue
            raise
        if not isinstance(operation, dict):
            raise ValueError("VS Code mutation entry is not an object")
        operations.append(operation)
    if not operations or operations[0].get("kind") not in (0, "initial"):
        raise ValueError("VS Code mutation log has no initial state")
    state: Any = {}
    for operation in operations:
        kind = operation.get("kind")
        if kind in (0, "initial"):
            state = deepcopy(operation.get("v", operation.get("value", {})))
            continue
        path_value = operation.get("k", operation.get("path", []))
        if not isinstance(path_value, list):
            raise ValueError("VS Code mutation path is invalid")
        if kind in (1, "set"):
            replacement = deepcopy(operation.get("v", operation.get("value")))
            if not path_value:
                state = replacement
            else:
                parent = _mutation_container(state, path_value[:-1])
                key = path_value[-1]
                if isinstance(parent, list):
                    parent[int(key)] = replacement
                elif isinstance(parent, dict):
                    parent[key] = replacement
                else:
                    raise ValueError("VS Code set target is invalid")
        elif kind in (2, "push"):
            target = _mutation_container(state, path_value)
            if not isinstance(target, list):
                raise ValueError("VS Code push target is not an array")
            values = operation.get("v", operation.get("value", []))
            if values is None:
                values = []
            if not isinstance(values, list):
                raise ValueError("VS Code push value is not an array")
            index = operation.get("i", operation.get("index"))
            if index is None:
                target.extend(deepcopy(values))
            else:
                target[int(index):] = deepcopy(values)
        elif kind in (3, "delete"):
            if not path_value:
                state = {}
            else:
                parent = _mutation_container(state, path_value[:-1])
                key = path_value[-1]
                if isinstance(parent, list):
                    del parent[int(key)]
                elif isinstance(parent, dict):
                    parent.pop(key, None)
                else:
                    raise ValueError("VS Code delete target is invalid")
        else:
            raise ValueError(f"unknown VS Code mutation kind: {kind}")
    if not isinstance(state, dict):
        raise ValueError("VS Code mutation log did not produce an object")
    return state, {"format": "objectMutationLog", "operationCount": len(operations),
                   "ignoredTornLines": torn}


def _vscode_state(path: Path) -> tuple[dict, dict[str, Any]]:
    if path.suffix == ".jsonl":
        return _vscode_replay(path)
    value = _read_json(path)
    if value is None:
        raise ValueError("VS Code chat session is invalid")
    return value, {"format": "legacy-json", "operationCount": 0,
                   "ignoredTornLines": 0}


def _vscode_title(state: dict, path: Path) -> str:
    if state.get("customTitle"):
        return str(state["customTitle"])
    for request in state.get("requests") or []:
        if not isinstance(request, dict):
            continue
        message = request.get("message")
        title = _text(message)
        if not title and isinstance(message, dict):
            title = _text(message.get("text"))
        if title:
            return title
    return str(state.get("sessionId") or path.stem)


def _vscode_project(value: Any, home: Path) -> str:
    if isinstance(value, dict):
        value = value.get("fsPath") or value.get("path") or value.get("external")
    return _display_path(str(value or ""), home)


def _vscode_paths(home: Path) -> list[Path]:
    paths: list[Path] = []
    app_support = home / "Library" / "Application Support"
    for editor in ("Code", "Code - Insiders"):
        user = app_support / editor / "User"
        workspace = user / "workspaceStorage"
        if workspace.is_dir():
            paths.extend(sorted(workspace.glob("*/chatSessions/*.jsonl")))
            paths.extend(sorted(workspace.glob("*/chatSessions/*.json")))
        empty = user / "globalStorage" / "emptyWindowChatSessions"
        if empty.is_dir():
            paths.extend(sorted(empty.glob("*.jsonl")))
            paths.extend(sorted(empty.glob("*.json")))
        transferred = user / "globalStorage" / "transferredChatSessions"
        if transferred.is_dir():
            paths.extend(sorted(transferred.glob("*.json")))
    return list(dict.fromkeys(paths))


def scan_vscode(home: Path) -> ProviderResult:
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    seen: set[str] = set()
    errors: list[str] = []
    for path in _vscode_paths(home):
        try:
            state, _ = _vscode_state(path)
            session_id = str(state.get("sessionId") or path.stem)
            if session_id in seen:
                continue
            seen.add(session_id)
            requests = [
                request for request in (state.get("requests") or [])
                if isinstance(request, dict)
            ]
            stamps = [_timestamp(state.get("creationDate"))]
            stamps.extend(
                _timestamp(request.get("timestamp")) for request in requests
            )
            mtime = max((*stamps, path.stat().st_mtime if not any(stamps) else 0))
            input_state = state.get("inputState")
            input_state = input_state if isinstance(input_state, dict) else {}
            selected_model = input_state.get("selectedModel")
            mode = input_state.get("mode")
            sid = _sid("vscode", f"{path.resolve()}:{session_id}")
            entry: dict[str, Any] = {
                "id": sid, "source": "vscode", "format": "vscode",
                "project": _vscode_project(state.get("workingDirectory"), home),
                "title": _vscode_title(state, path)[:120],
                "mtime": mtime, "size": path.stat().st_size,
                "version": state.get("version"),
                "sessionId": session_id,
                "creationDate": state.get("creationDate"),
                "workingDirectory": state.get("workingDirectory"),
                "requestCount": len(requests),
            }
            _apply_native(entry, session_id)
            if selected_model is not None:
                entry["model"] = selected_model
            if mode is not None:
                entry["mode"] = mode
            sessions.append(entry)
            targets[sid] = SessionTarget("vscode", path, session_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {exc}")
    if errors:
        return _result(
            "vscode", sessions, targets,
            "VS Code session unavailable: " + "; ".join(errors), "warning",
        )
    return _result("vscode", sessions, targets)


SCANNERS: tuple[tuple[str, Callable[[Path], ProviderResult]], ...] = (
    ("claude", scan_claude),
    ("codex", scan_codex),
    ("pi", scan_pi),
    ("prime", scan_prime),
    ("commandcode", scan_commandcode),
    ("cowork", scan_cowork),
    ("copilot", scan_copilot),
    ("cline", scan_cline),
    ("roo", scan_roo),
    ("goose", scan_goose),
    ("vscode", scan_vscode),
    ("gemini", scan_gemini),
    ("opencode", scan_opencode),
    ("cursor", scan_cursor),
    ("cursor-cli", scan_cursor_cli),
    ("kimi", scan_kimi),
    ("amp", scan_amp),
    ("qwen", scan_qwen),
    ("aider", scan_aider),
    ("trae", scan_trae),
    ("kiro", scan_kiro),
    ("kilo", lambda home: _scan_extension_tasks(home, "kilo")),
    ("zed", scan_zed),
)


def discover(home: Path | None = None) -> tuple[list[dict], dict[str, SessionTarget], list[dict]]:
    home = (home or Path.home()).expanduser()
    sessions: list[dict] = []
    targets: dict[str, SessionTarget] = {}
    statuses: list[dict] = []
    for source, scanner in SCANNERS:
        try:
            result = scanner(home)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
            statuses.append({
                "source": source, "count": 0, "status": "error",
                "message": f"{source} discovery failed: {exc}",
            })
            continue
        sessions.extend(result.sessions)
        targets.update(result.targets)
        statuses.append(result.status)
    sessions.sort(key=lambda item: item["mtime"], reverse=True)
    return sessions, targets, statuses


SOURCES = frozenset(name for name, _scanner in SCANNERS)


def _split_source_prefix(query: str) -> tuple[str | None, str]:
    if ":" not in query:
        return None, query
    prefix, rest = query.split(":", 1)
    if prefix.lower() in SOURCES and rest:
        return prefix.lower(), rest
    return None, query


def resolve_session_entry(query: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Match an opaque hash, native provider id, or ``source:native_id``.

    Opaque hashes win. A native id that matches more than one session raises
    :class:`AmbiguousSessionId` so the caller can ask for a source prefix
    instead of guessing from titles.
    """
    query = (query or "").strip()
    if not query:
        raise KeyError(query)
    lowered = query.lower()
    exact = [entry for entry in sessions if str(entry.get("id") or "") == query]
    if len(exact) == 1:
        return exact[0]
    source_filter, native_query = _split_source_prefix(query)
    needle = native_query.lower()
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in sessions:
        if source_filter and str(entry.get("source") or "").lower() != source_filter:
            continue
        opaque = str(entry.get("id") or "")
        if opaque.lower() == needle or any(key.lower() == needle or key.lower() == lowered
                                           for key in _native_keys(entry)):
            if opaque not in seen:
                seen.add(opaque)
                matches.append(entry)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousSessionId(query, matches)
    raise KeyError(query)


def resolve_session_id(
    query: str,
    sessions: list[dict[str, Any]],
    targets: dict[str, SessionTarget],
) -> tuple[str, SessionTarget]:
    """Return ``(opaque id, target)`` for an opaque or native session id."""
    entry = resolve_session_entry(query, sessions)
    opaque = entry["id"]
    target = targets.get(opaque)
    if target is None:
        raise KeyError(query)
    return opaque, target


def _ordered_files(root: Path, session_id: str, directory: str) -> list[tuple[Path, dict]]:
    base = root / directory
    found: list[tuple[Path, dict]] = []
    if not base.is_dir():
        return found
    for path in sorted(base.rglob("*.json")):
        data = _read_json(path)
        recorded_session = str(data.get("sessionID") or data.get("sessionId") or "") if data else ""
        grouped_message = directory == "message" and session_id in path.relative_to(base).parts[:-1]
        if data and (recorded_session == session_id or grouped_message):
            found.append((path, data))
    return found


def _sort_object(item: tuple[Path, dict]) -> tuple[float, str]:
    path, data = item
    value = data.get("time")
    if isinstance(value, dict):
        stamp = _timestamp(value.get("created") or value.get("updated"))
    else:
        stamp = _timestamp(data.get("createdAt") or data.get("time_created"))
    return stamp, str(path)


def _load_opencode_legacy(target: SessionTarget) -> str:
    session = _read_json(target.path)
    if session is None:
        raise FileNotFoundError("OpenCode session is unavailable")
    root = Path(target.extra[0])
    messages = _ordered_files(root, target.key, "message")
    parts = _ordered_files(root, target.key, "part")
    parts_by_message: dict[str, list[tuple[Path, dict]]] = {}
    for part in parts:
        message_id = str(part[1].get("messageID") or part[1].get("messageId") or "")
        parts_by_message.setdefault(message_id, []).append(part)
    archive = {"session": session, "messages": []}
    for _, message in sorted(messages, key=_sort_object):
        message_id = str(message.get("id") or "")
        ordered_parts = [
            data for _, data in sorted(parts_by_message.get(message_id, []), key=_sort_object)
        ]
        archive["messages"].append({"message": message, "parts": ordered_parts})
    return json.dumps(archive, separators=(",", ":"), ensure_ascii=False)


def _load_opencode_sqlite(target: SessionTarget) -> str:
    with _sqlite_ro(target.path) as connection:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        session_columns = _table_columns(connection, "session")
        row = connection.execute(
            f'SELECT * FROM "session" WHERE "{("id" if "id" in session_columns else "session_id")}" = ?',
            (target.key,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError("OpenCode session is unavailable")
        session = _row_object(row)
        messages: list[sqlite3.Row] = []
        if "message" in tables:
            message_columns = _table_columns(connection, "message")
            session_column = next(
                (name for name in ("session_id", "sessionID", "sessionId") if name in message_columns),
                None,
            )
            order = "time_created, id" if "time_created" in message_columns else "id"
            if session_column:
                messages = list(connection.execute(
                    f'SELECT * FROM "message" WHERE "{session_column}" = ? ORDER BY {order}',
                    (target.key,),
                ))
            else:
                messages = [
                    candidate for candidate in connection.execute(
                        f'SELECT * FROM "message" ORDER BY {order}'
                    )
                    if str(_row_object(candidate).get("sessionID") or "") == target.key
                ]
        parts: list[sqlite3.Row] = []
        if "part" in tables:
            part_columns = _table_columns(connection, "part")
            part_session_column = next(
                (name for name in ("session_id", "sessionID", "sessionId") if name in part_columns),
                None,
            )
            part_order = "time_created, id" if "time_created" in part_columns else "id"
            if part_session_column:
                parts = list(connection.execute(
                    f'SELECT * FROM "part" WHERE "{part_session_column}" = ? ORDER BY {part_order}',
                    (target.key,),
                ))
            else:
                message_ids = {str(_row_object(message).get("id") or "") for message in messages}
                parts = [
                    candidate for candidate in connection.execute(
                        f'SELECT * FROM "part" ORDER BY {part_order}'
                    )
                    if str(
                        _row_object(candidate).get("message_id") or
                        _row_object(candidate).get("messageID") or ""
                    ) in message_ids
                ]
    by_message: dict[str, list[dict]] = {}
    for row in parts:
        value = _row_object(row)
        message_id = str(value.get("message_id") or value.get("messageID") or "")
        by_message.setdefault(message_id, []).append(value)
    archive = {"session": session, "messages": []}
    for row in messages:
        message = _row_object(row)
        message_id = str(message.get("id") or "")
        archive["messages"].append({
            "message": message, "parts": by_message.get(message_id, []),
        })
    return json.dumps(archive, separators=(",", ":"), ensure_ascii=False, default=str)


def _load_cursor(target: SessionTarget) -> str:
    with _sqlite_ro(target.path) as connection:
        table = target.extra[0] if target.extra else _cursor_store(connection)
        composer = _cursor_get(connection, table, f"composerData:{target.key}")
        if composer is None:
            raise FileNotFoundError("Cursor conversation is unavailable")
        headers, inline = _cursor_conversation(composer)
        bubbles: list[dict] = []
        for header in headers:
            bubble = header if inline else _cursor_get(
                connection, table, f"bubbleId:{target.key}:{header.get('bubbleId')}"
            )
            if bubble:
                bubbles.append(_cursor_selected(bubble, CURSOR_BUBBLE_FIELDS))
    selected = _cursor_selected(composer, CURSOR_COMPOSER_FIELDS)
    selected.pop("fullConversationHeadersOnly", None)
    return json.dumps(
        {"composer": selected, "bubbles": bubbles},
        separators=(",", ":"), ensure_ascii=False,
    )


def _read_json_value(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_extension_task(target: SessionTarget) -> str:
    metadata_name = target.extra[0] if target.extra else "task_metadata.json"
    metadata = _read_json_value(target.path / metadata_name, {})
    api_messages = _read_json_value(
        target.path / "api_conversation_history.json", []
    )
    ui_messages = _read_json_value(target.path / "ui_messages.json", [])
    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(api_messages, list):
        api_messages = []
    if not isinstance(ui_messages, list):
        ui_messages = []
    return json.dumps(
        {
            "metadata": metadata,
            "apiMessages": api_messages,
            "uiMessages": ui_messages,
        },
        separators=(",", ":"), ensure_ascii=False,
    )


def _load_goose(target: SessionTarget) -> str:
    with _sqlite_ro(target.path) as connection:
        session_table = target.extra[0] if target.extra else _goose_table(
            connection, "sessions"
        )
        if session_table is None:
            raise FileNotFoundError("Goose session table is unavailable")
        session_columns = _table_columns(connection, session_table)
        id_column = (
            target.extra[1] if len(target.extra) > 1 else
            "id" if "id" in session_columns else "session_id"
        )
        selected = _selected_columns(session_columns, GOOSE_SESSION_FIELDS)
        fields = ", ".join(f'"{column}"' for column in selected)
        row = connection.execute(
            f'SELECT {fields} FROM "{session_table}" WHERE "{id_column}" = ?',
            (target.key,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError("Goose session is unavailable")
        session = _decode_json_columns(row)

        messages: list[dict[str, Any]] = []
        message_table = _goose_table(connection, "messages")
        if message_table is not None:
            columns = _table_columns(connection, message_table)
            session_column = next(
                (name for name in ("session_id", "sessionId") if name in columns),
                None,
            )
            selected = _selected_columns(columns, GOOSE_MESSAGE_FIELDS)
            if session_column and selected:
                fields = ", ".join(f'"{column}"' for column in selected)
                order_column = next(
                    (name for name in ("created_timestamp", "created_at",
                                      "message_id", "id") if name in columns),
                    session_column,
                )
                rows = connection.execute(
                    f'SELECT {fields} FROM "{message_table}" '
                    f'WHERE "{session_column}" = ? ORDER BY "{order_column}"',
                    (target.key,),
                )
                messages = [_decode_json_columns(item) for item in rows]

        usage: list[dict[str, Any]] = []
        if "usage_ledger" in _tables(connection):
            columns = _table_columns(connection, "usage_ledger")
            session_column = next(
                (name for name in ("session_id", "sessionId") if name in columns),
                None,
            )
            if session_column:
                rows = connection.execute(
                    f'SELECT * FROM "usage_ledger" WHERE "{session_column}" = ?',
                    (target.key,),
                )
                usage = [_decode_json_columns(item) for item in rows]
    return json.dumps(
        {"session": session, "messages": messages, "usage": usage},
        separators=(",", ":"), ensure_ascii=False, default=str,
    )


def _load_vscode(target: SessionTarget) -> str:
    state, diagnostics = _vscode_state(target.path)
    return json.dumps(
        {"session": state, "diagnostics": diagnostics},
        separators=(",", ":"), ensure_ascii=False,
    )


def _load_kimi(target: SessionTarget) -> str:
    """Merge a Kimi conversation into a bounded synthetic archive.

    ``context.jsonl`` carries the message log (user/assistant/tool roles);
    ``wire.jsonl`` carries ``ToolCall`` events with the tool name and
    arguments, keyed by the same ids the tool results reference.
    """
    calls: dict[str, dict[str, Any]] = {}
    if target.extra:
        wire = Path(target.extra[0])
        if wire.is_file():
            with wire.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = obj.get("message") if isinstance(obj, dict) else None
                    payload = message.get("payload") if isinstance(message, dict) else None
                    if isinstance(payload, dict) and message.get("type") == "ToolCall":
                        function = payload.get("function") or {}
                        calls[str(payload.get("id") or "")] = {
                            "name": str(function.get("name") or ""),
                            "arguments": function.get("arguments"),
                        }
    messages: list[dict] = []
    with target.path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = obj.get("role")
            if role in ("_checkpoint", "_usage"):
                continue
            message: dict[str, Any] = {"role": role}
            content = obj.get("content")
            if isinstance(content, list):
                blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "think":
                        blocks.append({"type": "thinking", "thinking": block.get("think", "")})
                    elif block.get("type") == "text":
                        blocks.append({"type": "text", "text": block.get("text", "")})
                    else:
                        blocks.append(block)
                message["content"] = blocks
            else:
                message["content"] = content or ""
            if role == "tool":
                call = calls.get(str(obj.get("tool_call_id") or ""))
                if call:
                    message["toolName"] = call["name"]
                    message["arguments"] = call["arguments"]
            messages.append({"message": message})
    return json.dumps(
        {"kimiArchive": True, "session": {}, "messages": messages},
        separators=(",", ":"), ensure_ascii=False,
    )


def load_target(target: SessionTarget) -> str:
    if target.kind == "file":
        return target.path.read_text(encoding="utf-8", errors="replace")
    if target.kind == "extension-task":
        return _load_extension_task(target)
    if target.kind == "goose":
        return _load_goose(target)
    if target.kind == "vscode":
        return _load_vscode(target)
    if target.kind == "gemini-jsonl":
        return json.dumps(
            _gemini_jsonl(target.path),
            separators=(",", ":"), ensure_ascii=False,
        )
    if target.kind == "opencode-legacy":
        return _load_opencode_legacy(target)
    if target.kind == "opencode-sqlite":
        return _load_opencode_sqlite(target)
    if target.kind == "cursor":
        return _load_cursor(target)
    if target.kind == "kimi":
        return _load_kimi(target)
    if target.kind == "amp":
        return _load_amp(target)
    if target.kind == "trae":
        return _load_trae(target)
    if target.kind == "zed":
        return _load_zed(target)
    raise ValueError(f"unsupported session target: {target.kind}")
