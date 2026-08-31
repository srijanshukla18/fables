#!/usr/bin/env python3
"""
install-mcp.py — register the Fables MCP server with every coding agent
Fables supports, so each agent can list, fetch, and search the agent
sessions stored on this machine.

Targets (each idempotent, with one-shot ``<file>.fables.bak`` backups):

    codex     ~/.codex/config.toml                      [mcp_servers.fables]
    claude    ~/.claude.json                            mcpServers.fables
    gemini    ~/.gemini/settings.json                   mcpServers.fables
    cursor    ~/.cursor/mcp.json                        mcpServers.fables
    opencode  ~/.config/opencode/opencode.json(c)       mcp.fables
    cline     ~/.cline/data/settings/cline_mcp_settings.json (+ editor storage)
    roo       editor global-storage mcp_settings.json (Code/Insiders/Cursor/Windsurf)
    vscode    ~/Library/Application Support/Code[- Insiders]/User/mcp.json
    goose     ~/.config/goose/config.yaml               extensions.fables
    copilot   copilot mcp add (CLI, when available)
    commandcode  cmd mcp add -s user (CLI, when available)
    amp        amp mcp add (CLI, when available)
    qwen       ~/.qwen/settings.json mcpServers
    trae       ~/Library/Application Support/Trae/User/mcp.json
    kiro       ~/.kiro/settings/mcp.json
    kilo       ~/.config/kilo/kilo.jsonc (mcp key) + legacy extension storage
    zed        ~/.config/zed/settings.json (context_servers)
    prime     ~/.prime/agent/settings.json (HTTP endpoint + kernel skill)
    hermes    ~/.hermes/config.yaml                      mcp_servers.fables
    pi        ~/.pi/agent/extensions/fables-mcp.ts + fables-mcp.json

The server is launched through ``uv run`` when uv is available (it manages
its own Python), otherwise the current Python interpreter is used.

Usage:
    python3 install-mcp.py                  # register everywhere (default)
    python3 install-mcp.py --check          # report status without writing
    python3 install-mcp.py --remove         # unregister everywhere
    python3 install-mcp.py --home DIR       # operate on a different home
    python3 install-mcp.py --server-cmd CMD --server-args A,B   # explicit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "fables"
BACKUP_SUFFIX = ".fables.bak"
EDITORS = ("Code", "Code - Insiders", "Cursor", "Windsurf")


class EditError(Exception):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _q(value: str) -> str:
    """Quote a string for TOML/YAML/JSON-style basic strings."""
    return json.dumps(value, ensure_ascii=False)


def backup(path: Path) -> None:
    if not path.exists():
        return
    destination = Path(str(path) + BACKUP_SUFFIX)
    if not destination.exists():
        shutil.copy2(path, destination)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def json_load(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EditError(f"{path} is not a JSON object")
    return data


def json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def json_has(path: Path, key: str, name: str) -> bool:
    data = json_load(path)
    container = data.get(key)
    return isinstance(container, dict) and name in container


def json_install(path: Path, key: str, name: str, entry: dict) -> str:
    data = json_load(path)
    container = data.setdefault(key, {})
    if not isinstance(container, dict):
        raise EditError(f"{path}: {key!r} is not an object")
    if name in container:
        return "exists"
    container[name] = entry
    backup(path)
    json_write(path, data)
    return "ok"


def json_remove(path: Path, key: str, name: str) -> str:
    data = json_load(path)
    container = data.get(key)
    if not isinstance(container, dict) or name not in container:
        return "absent"
    del container[name]
    if not container:
        data.pop(key, None)
    backup(path)
    json_write(path, data)
    return "removed"


def toml_install(path: Path, command: str, argv: list[str]) -> str:
    text = read_text(path)
    if re.search(r"^\[mcp_servers\.fables\]\s*$", text, re.M):
        return "exists"
    args = "[" + ", ".join(_q(item) for item in argv) + "]"
    body = (f"\n[mcp_servers.fables]\n"
            f'command = {_q(command)}\n'
            f'args = {args}\n')
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(body if text.endswith("\n") else "\n" + body)
    return "ok"


def toml_remove(path: Path) -> str:
    lines = read_text(path).splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == "[mcp_servers.fables]"), None)
    if start is None:
        return "absent"
    end = start + 1
    while end < len(lines) and not lines[end].startswith("["):
        end += 1
    backup(path)
    remaining = lines[:start] + lines[end:]
    while remaining and not remaining[-1].strip():
        remaining.pop()
    path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    return "removed"


def yaml_install(path: Path, name: str, block: str, root: str = "extensions") -> str:
    lines = read_text(path).splitlines()
    index = next((i for i, line in enumerate(lines)
                  if re.match(rf"^{re.escape(root)}:\s*(?:\{{\}}|null)?\s*$", line)), None)
    if index is not None:
        if lines[index].strip() != f"{root}:":
            lines[index] = f"{root}:"
        for line in lines[index + 1:]:
            if not line.strip():
                continue
            if not line.startswith(" "):
                break
            if line == f"  {name}:":
                return "exists"
        backup(path)
        lines.insert(index + 1, block)
    else:
        backup(path)
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{root}:")
        lines.append(block)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "ok"


def yaml_remove(path: Path, name: str, root: str = "extensions") -> str:
    lines = read_text(path).splitlines()
    index = next((i for i, line in enumerate(lines)
                  if re.match(rf"^{re.escape(root)}:\s*$", line)), None)
    if index is None:
        return "absent"
    start = next((i for i in range(index + 1, len(lines))
                  if lines[i] == f"  {name}:"), None)
    if start is None:
        return "absent"
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if not line.strip() or line.startswith("    "):
            end += 1
        else:
            break
    backup(path)
    remaining = lines[:start] + lines[end:]
    root_end = index + 1
    while root_end < len(remaining):
        line = remaining[root_end]
        if line.strip() and not line.startswith(" "):
            break
        root_end += 1
    if not any(
        line.startswith("  ") and line.strip()
        for line in remaining[index + 1:root_end]
    ):
        remaining = remaining[:index] + remaining[index + 1:]
    while remaining and not remaining[-1].strip():
        remaining.pop()
    path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    return "removed"


def jsonc_block(text: str, key: str) -> tuple[int, int] | None:
    """Return (start, end) of the object assigned to ``key``, braces included."""
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', text)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, index
    return None


def jsonc_install(path: Path, key: str, name: str, entry_text: str) -> str:
    text = read_text(path)
    if not text.strip():
        text = "{\n}\n"
    block = jsonc_block(text, key)
    if block is not None:
        inner = text[block[0] + 1:block[1]]
        if re.search(r'"' + re.escape(name) + r'"\s*:', inner):
            return "exists"
        insertion = f"\n    \"{name}\": {entry_text}," if inner.strip() \
            else f"\n    \"{name}\": {entry_text}"
        backup(path)
        text = text[:block[0] + 1] + insertion + text[block[0] + 1:]
    else:
        if re.search(r'"' + re.escape(name) + r'"\s*:', text):
            return "exists"
        stripped = text.rstrip()
        if not stripped.endswith("}"):
            raise EditError(f"{path}: cannot locate the root object")
        insertion = f',\n  "{key}": {{\n    "{name}": {entry_text}\n  }}'
        backup(path)
        text = stripped[:-1] + insertion + "\n}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "ok"


def jsonc_remove(path: Path, key: str, name: str) -> str:
    text = read_text(path)
    block = jsonc_block(text, key)
    if block is None:
        return "absent"
    inner_start, inner_end = block[0] + 1, block[1]
    match = re.search(
        r',?\s*"' + re.escape(name) + r'"\s*:\s*\{[^{}]*\}\s*,?',
        text[inner_start:inner_end],
    )
    if not match:
        return "absent"
    backup(path)
    replaced = text[inner_start:inner_end][:match.start()] + \
        text[inner_start:inner_end][match.end():]
    replaced = re.sub(r",\s*\n(\s*)\}", r"\n\1}", replaced)
    path.write_text(text[:inner_start] + replaced + text[inner_end:],
                    encoding="utf-8")
    return "removed"


def run(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        process = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
        return process.returncode, (process.stderr or process.stdout).strip()
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


# ---------------------------------------------------------------------------
# Server resolution
# ---------------------------------------------------------------------------

def find_script(home: Path) -> Path | None:
    candidates = []
    env_dir = os.environ.get("FABLES_INSTALL_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / "fables-mcp.py")
    candidates.append(home / ".local" / "share" / "fables" / "fables-mcp.py")
    candidates.append(Path(__file__).resolve().parent / "fables-mcp.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def resolve_server(args: argparse.Namespace, home: Path) -> tuple[str, list[str]]:
    if args.server_cmd:
        command = args.server_cmd
        argv = [item for item in args.server_args.split(",") if item]
        return command, argv
    script = find_script(home)
    if script is None:
        raise EditError(
            "fables-mcp.py not found — run install.sh first, or pass "
            "--server-cmd/--server-args explicitly"
        )
    uv = shutil.which("uv")
    if uv:
        return uv, ["run", str(script)]
    return sys.executable, [str(script)]


def display_server(command: str, argv: list[str]) -> str:
    return " ".join([command, *argv])


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def agent_codex(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    path = home / ".codex" / "config.toml"
    command, argv = server
    if mode == "check":
        return ("registered" if toml_has(path) else "not registered", str(path))
    if mode == "remove":
        return toml_remove(path), str(path)
    return toml_install(path, command, argv), str(path)


def toml_has(path: Path) -> bool:
    return re.search(r"^\[mcp_servers\.fables\]\s*$", read_text(path), re.M) is not None


def json_agent(path: Path, key: str, entry: dict, mode: str,
               name: str = SERVER_NAME) -> tuple[str, str]:
    if mode == "check":
        return ("registered" if json_has(path, key, name) else "not registered", str(path))
    if mode == "remove":
        return json_remove(path, key, name), str(path)
    return json_install(path, key, name, entry), str(path)


def stdio_entry(command: str, argv: list[str]) -> dict:
    return {"type": "stdio", "command": command, "args": list(argv)}


def agent_claude(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    return json_agent(home / ".claude.json", "mcpServers",
                      stdio_entry(command, argv), mode)


def agent_gemini(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    return json_agent(home / ".gemini" / "settings.json", "mcpServers",
                      {"command": command, "args": list(argv)}, mode)


def agent_cursor(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    entry = stdio_entry(command, argv)
    # Cursor runs stdio MCP servers in a sandbox that may deny access to uv's
    # default cache under ~/.cache. Keep uv's state in the system temp directory,
    # which is writable from the sandbox.
    entry["env"] = {"UV_CACHE_DIR": "/private/tmp/fables-uv-cache"}
    return json_agent(home / ".cursor" / "mcp.json", "mcpServers", entry, mode)


def agent_opencode(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    path = None
    for candidate in (home / ".config" / "opencode" / "opencode.jsonc",
                      home / ".config" / "opencode" / "opencode.json",
                      home / ".opencode.json"):
        if candidate.exists():
            path = candidate
            break
    if path is None:
        path = home / ".config" / "opencode" / "opencode.jsonc"
    entry = json.dumps({"type": "stdio", "command": command,
                        "args": list(argv), "enabled": True}, ensure_ascii=False)
    if mode == "check":
        return ("registered" if jsonc_block(read_text(path), "mcp") and
                re.search(r'"fables"\s*:', read_text(path)) else "not registered",
                str(path))
    if mode == "remove":
        return jsonc_remove(path, "mcp", SERVER_NAME), str(path)
    return jsonc_install(path, "mcp", SERVER_NAME, entry), str(path)


def editor_storage_dirs(home: Path, extension: str) -> list[Path]:
    found = []
    for editor in EDITORS:
        storage = (home / "Library" / "Application Support" / editor /
                   "User" / "globalStorage" / extension / "settings")
        if storage.is_dir():
            found.append(storage)
    return found


def agent_cline(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    entry = {"command": command, "args": list(argv),
             "disabled": False, "autoApprove": []}
    results = []
    primary = home / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
    results.append(json_agent(primary, "mcpServers", entry, mode))
    for storage in editor_storage_dirs(home, "saoudrizwan.claude-dev"):
        results.append(json_agent(storage / "cline_mcp_settings.json",
                                  "mcpServers", entry, mode))
    statuses = {status for status, _ in results}
    if mode == "check":
        status = "registered" if "registered" in statuses else "not registered"
    elif mode == "remove":
        status = "removed" if "removed" in statuses else "absent"
    else:
        status = "ok" if "ok" in statuses else "exists" if "exists" in statuses else "ok"
    return status, str(primary)


def agent_roo(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    entry = stdio_entry(command, argv)
    entry["disabled"] = False
    results = []
    for storage in editor_storage_dirs(home, "rooveterinaryinc.roo-cline"):
        results.append(json_agent(storage / "mcp_settings.json",
                                  "mcpServers", entry, mode))
    if not results:
        return ("skipped" if mode != "remove" else "absent",
                "no Roo Code extension storage found")
    statuses = {status for status, _ in results}
    if mode == "check":
        status = "registered" if "registered" in statuses else "not registered"
    elif mode == "remove":
        status = "removed" if "removed" in statuses else "absent"
    else:
        status = "ok" if "ok" in statuses else "exists" if "exists" in statuses else "ok"
    return status, str(results[0][1])


def agent_vscode(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    entry = {"command": command, "args": list(argv)}
    results = []
    for editor in ("Code", "Code - Insiders"):
        user_dir = home / "Library" / "Application Support" / editor / "User"
        if not user_dir.is_dir() and mode != "remove":
            continue
        results.append(json_agent(user_dir / "mcp.json", "servers", entry, mode))
    if not results:
        return ("skipped" if mode != "remove" else "absent",
                "no VS Code user directory found")
    statuses = {status for status, _ in results}
    if mode == "check":
        status = "registered" if "registered" in statuses else "not registered"
    elif mode == "remove":
        status = "removed" if "removed" in statuses else "absent"
    else:
        status = "ok" if "ok" in statuses else "exists" if "exists" in statuses else "ok"
    return status, str(results[0][1])


def goose_block(command: str, argv: list[str]) -> str:
    lines = ["  fables:", "    type: stdio", "    name: fables",
             "    enabled: true", f"    cmd: {_q(command)}", "    args:"]
    for item in argv:
        lines.append(f"      - {_q(item)}")
    lines += ["    envs: {}", "    env_keys: []", "    timeout: 300"]
    return "\n".join(lines)


def agent_goose(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    paths = [home / ".config" / "goose" / "config.yaml"]
    mac_block = home / "Library" / "Application Support" / "Block" / "goose" / "config.yaml"
    if mac_block.parent.is_dir():
        paths.append(mac_block)
    results = []
    block = goose_block(command, argv)
    for path in paths:
        if mode == "check":
            status = "registered" if re.search(
                r"^  fables:", read_text(path), re.M) else "not registered"
            results.append((status, str(path)))
        elif mode == "remove":
            results.append((yaml_remove(path, SERVER_NAME), str(path)))
        else:
            results.append((yaml_install(path, SERVER_NAME, block), str(path)))
    statuses = {status for status, _ in results}
    if mode == "check":
        status = "registered" if "registered" in statuses else "not registered"
    elif mode == "remove":
        status = "removed" if "removed" in statuses else "absent"
    else:
        status = "ok" if "ok" in statuses else "exists" if "exists" in statuses else "ok"
    return status, str(paths[0])


def hermes_block(command: str, argv: list[str]) -> str:
    lines = ["  fables:", f"    command: {_q(command)}", "    args:"]
    for item in argv:
        lines.append(f"      - {_q(item)}")
    lines.append("    enabled: true")
    return "\n".join(lines)


def agent_hermes(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    path = home / ".hermes" / "config.yaml"
    if not path.exists() and mode != "remove" and shutil.which("hermes") is None:
        return "skipped", "Hermes Agent not found"
    if mode == "check":
        registered = False
        lines = read_text(path).splitlines()
        root = next((i for i, line in enumerate(lines)
                     if re.match(r"^mcp_servers:\s*$", line)), None)
        if root is not None:
            for line in lines[root + 1:]:
                if line.strip() and not line.startswith(" "):
                    break
                if line == "  fables:":
                    registered = True
                    break
        return ("registered" if registered else "not registered", str(path))
    if mode == "remove":
        return yaml_remove(path, SERVER_NAME, "mcp_servers"), str(path)
    command, argv = server
    return yaml_install(
        path, SERVER_NAME, hermes_block(command, argv), "mcp_servers",
    ), str(path)


def copilot_status(path: Path) -> bool:
    return "fables" in read_text(path)


def agent_copilot(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    binary = shutil.which("copilot")
    config = home / ".copilot" / "mcp-config.json"
    if mode == "check":
        if binary is None:
            return "skipped", "copilot CLI not found"
        return ("registered" if copilot_status(config) else "not registered", str(config))
    if binary is None:
        return ("skipped" if mode != "remove" else "absent", "copilot CLI not found")
    command, argv = server
    if mode == "remove":
        code, message = run([binary, "mcp", "delete", SERVER_NAME])
        return (("removed" if code == 0 else "error"), message or str(config))
    code, message = run([binary, "mcp", "add", SERVER_NAME, "--", command, *argv])
    if code == 0:
        return "ok", str(config)
    if copilot_status(config):
        return "exists", str(config)
    return "error", message or "copilot mcp add failed"


def agent_commandcode(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    binary = shutil.which("cmd")
    if binary is None:
        return ("skipped" if mode != "remove" else "absent", "command-code CLI not found")
    command, argv = server
    if mode == "check":
        _code, out = run([binary, "mcp", "list"])
        return ("registered" if SERVER_NAME in out else "not registered",
                "cmd mcp list")
    if mode == "remove":
        code, message = run([binary, "mcp", "remove", SERVER_NAME])
        return (("removed" if code == 0 else "error"), message or "cmd mcp remove")
    code, message = run([binary, "mcp", "add", SERVER_NAME, "-s", "user",
                         "--", command, *argv])
    if code == 0:
        return "ok", "cmd mcp add -s user"
    _code, out = run([binary, "mcp", "list"])
    if SERVER_NAME in out:
        return "exists", "cmd mcp add (already registered)"
    return "error", message or "cmd mcp add failed"


PRIME_SKILL_FILES = {
    "SKILL.md": (
        "# fables\n\n"
        "Query local coding-agent session archives (pi, Prime Agent, Claude Code, "
        "Codex, Kimi CLI, Cursor CLI, ...) through the Fables MCP server.\n\n"
        "Use \"import fables\" in the kernel:\n\n"
        "- await fables.list_tools() — discover the available tools\n"
        "- await fables.list_sessions(source=\"pi\", limit=20) — newest sessions\n"
        "- await fables.get_session(id=..., include_thinking=False,\n"
        "  include_tools=False) — a transcript (id may be a list_sessions\n"
        "  hash, a native provider id such as a pi UUID, or source:native_id;\n"
        "  include_thinking and include_tools are independent)\n"
        "- await fables.search_sessions(query=..., include_thinking=False,\n"
        "  include_tools=False) — search\n\n"
        "The server runs locally at http://127.0.0.1:8322/mcp. If a call raises "
        "NotEnabled, start the server with \"fables mcp-http\" (or \"python3 "
        "fables-mcp.py --http\") and retry.\n"
    ),
    "pyproject.toml": (
        "[project]\n"
        "name = \"prime-agent-skill-fables\"\n"
        "version = \"0.1.0\"\n"
        "requires-python = \">=3.10\"\n"
        "dependencies = [\"mcp\", \"httpx\", \"prime-agent-runtime\"]\n\n"
        "[build-system]\n"
        "requires = [\"hatchling\"]\n"
        "build-backend = \"hatchling.build\"\n\n"
        "[tool.hatch.build.targets.wheel]\n"
        "packages = [\"src/fables\"]\n"
    ),
    "src/fables/__init__.py": (
        "from rlm import McpIntegration\n\n"
        "class Fables(McpIntegration):\n"
        "    server = \"fables\"\n"
        "    url = \"http://127.0.0.1:8322/mcp\"\n\n"
        "fables = Fables()\n\n"
        "_RESERVED = {\"run\", \"__wrapped__\", \"__call__\"}\n\n"
        "def __getattr__(name):\n"
        "    if name.startswith(\"_\") or name in _RESERVED:\n"
        "        raise AttributeError(name)\n"
        "    return getattr(fables, name)\n"
    ),
}


def agent_amp(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    binary = shutil.which("amp")
    if binary is None:
        return ("skipped" if mode != "remove" else "absent", "amp CLI not found")
    command, argv = server
    if mode == "check":
        _code, out = run([binary, "mcp", "doctor"])
        return ("registered" if SERVER_NAME in out else "not registered",
                "amp mcp doctor")
    if mode == "remove":
        code, message = run([binary, "mcp", "remove", SERVER_NAME])
        return (("removed" if code == 0 else "error"), message or "amp mcp remove")
    code, message = run([binary, "mcp", "add", SERVER_NAME, "--", command, *argv])
    if code == 0:
        return "ok", "amp mcp add"
    _code, out = run([binary, "mcp", "doctor"])
    if SERVER_NAME in out:
        return "exists", "amp mcp add (already registered)"
    return "error", message or "amp mcp add failed"


def agent_qwen(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    return json_agent(home / ".qwen" / "settings.json", "mcpServers",
                      {"command": command, "args": list(argv)}, mode)


def agent_trae(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    return json_agent(
        home / "Library" / "Application Support" / "Trae" / "User" / "mcp.json",
        "mcpServers", {"command": command, "args": list(argv)}, mode)


def agent_kiro(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    return json_agent(home / ".kiro" / "settings" / "mcp.json", "mcpServers",
                      {"command": command, "args": list(argv)}, mode)


def agent_kilo(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    command, argv = server
    entry = {"type": "local", "command": command, "args": list(argv)}
    results = []
    jsonc = home / ".config" / "kilo" / "kilo.jsonc"
    json_path = home / ".config" / "kilo" / "kilo.json"
    if jsonc.exists():
        config = jsonc
    elif json_path.exists():
        config = json_path
    else:
        config = jsonc  # canonical name for new configs
    entry_text = json.dumps(entry, ensure_ascii=False)
    if mode == "check":
        results.append(("registered" if jsonc_block(read_text(config), "mcp") and
                        re.search(r'"fables"\s*:', read_text(config))
                        else "not registered", str(config)))
    elif mode == "remove":
        results.append((jsonc_remove(config, "mcp", SERVER_NAME), str(config)))
    else:
        results.append((jsonc_install(config, "mcp", SERVER_NAME, entry_text),
                        str(config)))
    # Legacy VS Code extension storage, when present.
    for storage in editor_storage_dirs(home, "kilocode.kilo-code"):
        results.append(json_agent(storage / "mcp_settings.json", "mcpServers",
                                  {"command": command, "args": list(argv)}, mode))
    statuses = {status for status, _ in results}
    if mode == "check":
        status = "registered" if "registered" in statuses else "not registered"
    elif mode == "remove":
        status = "removed" if "removed" in statuses else "absent"
    else:
        status = "ok" if "ok" in statuses else "exists" if "exists" in statuses else "ok"
    return status, str(config)


def agent_zed(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    """Zed's settings.json is JSONC (comments allowed), so edits go through
    the comment-preserving jsonc path. Zed configures MCP servers under the
    ``context_servers`` key."""
    command, argv = server
    path = home / ".config" / "zed" / "settings.json"
    entry = json.dumps({"command": command, "args": list(argv)}, ensure_ascii=False)
    if mode == "check":
        registered = jsonc_block(read_text(path), "context_servers") and \
            re.search(r'"fables"\s*:', read_text(path))
        return ("registered" if registered else "not registered", str(path))
    if mode == "remove":
        return jsonc_remove(path, "context_servers", SERVER_NAME), str(path)
    return jsonc_install(path, "context_servers", SERVER_NAME, entry), str(path)


def agent_prime(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    """Prime Agent consumes MCP over HTTP only (its kernel drops stdio
    entries), so registration writes an HTTP endpoint into settings.json and
    ships a Python-backed skill that binds the server's tools in the kernel."""
    settings = home / ".prime" / "agent" / "settings.json"
    skill_dir = home / ".prime" / "agent" / "skills" / "fables"
    entry = {"type": "http", "url": "http://127.0.0.1:8322/mcp", "enabled": True}
    if mode == "check":
        registered = json_has(settings, "mcpServers", SERVER_NAME) and \
            (skill_dir / "SKILL.md").is_file()
        return ("registered" if registered else "not registered", str(settings))
    if mode == "remove":
        results = [json_remove(settings, "mcpServers", SERVER_NAME)]
        if skill_dir.exists():
            backup_dir = Path(str(skill_dir) + BACKUP_SUFFIX)
            if not backup_dir.exists():
                shutil.move(str(skill_dir), str(backup_dir))
            else:
                shutil.rmtree(skill_dir)
            results.append("removed")
        else:
            results.append("absent")
        status = "removed" if "removed" in results else "absent"
        return status, str(skill_dir)
    statuses = [json_install(settings, "mcpServers", SERVER_NAME, entry)]
    if not skill_dir.is_dir():
        backup_dir = Path(str(skill_dir) + BACKUP_SUFFIX)
        if backup_dir.is_dir():
            shutil.move(str(backup_dir), str(skill_dir))
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "src").mkdir(exist_ok=True)
    for name, content in PRIME_SKILL_FILES.items():
        target = skill_dir / name
        if target.exists() and target.read_text(encoding="utf-8") == content:
            statuses.append("exists")
            continue
        if not Path(str(target) + BACKUP_SUFFIX).exists() and target.exists():
            shutil.copy2(target, Path(str(target) + BACKUP_SUFFIX))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        statuses.append("ok")
    return ("ok" if "ok" in statuses else "exists", str(skill_dir))


def agent_pi(home: Path, server: tuple[str, list[str]], mode: str) -> tuple[str, str]:
    extension_dir = home / ".pi" / "agent" / "extensions"
    source = Path(__file__).resolve().parent / "fables-mcp.ts"
    if mode == "check":
        return ("registered" if (extension_dir / "fables-mcp.ts").exists()
                else "not registered", str(extension_dir / "fables-mcp.ts"))
    if mode == "remove":
        removed = []
        for name in ("fables-mcp.ts", "fables-mcp.json"):
            target = extension_dir / name
            if target.exists():
                backup(target)
                target.unlink()
                removed.append(name)
        return ("removed" if removed else "absent",
                str(extension_dir / "fables-mcp.ts"))
    if not extension_dir.is_dir():
        return "skipped", "no pi extensions directory (~/.pi/agent/extensions)"
    if not source.is_file():
        return "skipped", "fables-mcp.ts not found next to install-mcp.py"
    command, argv = server
    results = []
    target = extension_dir / "fables-mcp.ts"
    if target.exists() and target.read_text(encoding="utf-8") == \
            source.read_text(encoding="utf-8"):
        results.append("exists")
    else:
        backup(target)
        shutil.copy2(source, target)
        results.append("ok")
    sidecar = extension_dir / "fables-mcp.json"
    payload = {"cmd": command, "args": list(argv)}
    if sidecar.exists() and json_load(sidecar) == payload:
        results.append("exists")
    else:
        backup(sidecar)
        json_write(sidecar, payload)
        results.append("ok")
    return ("ok" if "ok" in results else "exists", str(target))


AGENTS = [
    ("codex", agent_codex, "Codex CLI"),
    ("claude", agent_claude, "Claude Code"),
    ("gemini", agent_gemini, "Gemini CLI"),
    ("cursor", agent_cursor, "Cursor"),
    ("opencode", agent_opencode, "OpenCode"),
    ("cline", agent_cline, "Cline"),
    ("roo", agent_roo, "Roo Code"),
    ("vscode", agent_vscode, "VS Code Chat"),
    ("goose", agent_goose, "Goose"),
    ("copilot", agent_copilot, "Copilot CLI"),
    ("commandcode", agent_commandcode, "Command Code"),
    ("amp", agent_amp, "Amp"),
    ("qwen", agent_qwen, "Qwen Code"),
    ("trae", agent_trae, "Trae"),
    ("kiro", agent_kiro, "Kiro"),
    ("kilo", agent_kilo, "Kilo Code"),
    ("zed", agent_zed, "Zed"),
    ("prime", agent_prime, "Prime Agent"),
    ("hermes", agent_hermes, "Hermes Agent"),
    ("pi", agent_pi, "pi (extension bridge)"),
]

# Agents Fables reads but that expose no user-level MCP configuration.
SKIPPED = [
    ("cowork", "Claude Cowork", "no user-level MCP configuration"),
    ("aider", "Aider", "no native MCP client support (aider issue #4506)"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register the Fables MCP server with every supported agent")
    parser.add_argument("--check", action="store_true",
                        help="report registration status without writing")
    parser.add_argument("--remove", action="store_true",
                        help="unregister fables everywhere")
    parser.add_argument("--home", default=None,
                        help="home directory to operate on (default: $HOME)")
    parser.add_argument("--server-cmd", default=None,
                        help="explicit server command (e.g. uv, python3)")
    parser.add_argument("--server-args", default="",
                        help="comma-separated server arguments (e.g. run,/path/fables-mcp.py)")
    args = parser.parse_args(argv)
    mode = "check" if args.check else "remove" if args.remove else "install"
    home = Path(args.home or os.environ.get("HOME", "")).expanduser()
    if not home.is_dir():
        print(f"error: home directory not found: {home}", file=sys.stderr)
        return 1
    try:
        server = resolve_server(args, home)
    except EditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    command, argv = server
    print(f"fables-mcp server: {display_server(command, argv)}")
    print()
    failures = 0
    for name, handler, label in AGENTS:
        try:
            status, detail = handler(home, server, mode)
        except EditError as exc:
            status, detail = "error", str(exc)
        mark = {"ok": "ok", "registered": "ok", "removed": "ok"}.get(status, "  ")
        if status == "error":
            failures += 1
        print(f"[{mark}] {name:<10} {label:<14} {status}  ({detail})")
    for name, label, reason in SKIPPED:
        print(f"[  ] {name:<10} {label:<14} skipped  ({reason})")
    print()
    if failures:
        print(f"{failures} target(s) failed — inspect the lines above.")
        return 1
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
