"""Register a stdio MCP server with every agent client found on this machine.

Stdlib only, shared verbatim by trimtok and memcore. Each client keeps its own
config file and format; this module knows where each lives and how to edit it
without clobbering anything else in the file:

* JSON configs are parsed, one key is set, and the file is replaced atomically.
  A config that does not parse (comments, trailing commas) is never rewritten;
  the client is reported as ``skipped`` and a snippet is printed instead.
* Codex's TOML is edited textually (our own table only) and re-validated with
  ``tomllib`` when available before it is written.
* Claude Code is driven through its own ``claude mcp`` CLI when it is on PATH,
  because ``~/.claude.json`` is rewritten live by the running app.

The server is launched as ``<this python> -m <module>``: no PATH lookup, no
reliance on console-script shims, and it keeps working inside venv/pipx/uv.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:  # Python 3.11+
    import tomllib as _tomllib  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - older interpreters skip validation
    _tomllib = None


@dataclass
class ServerSpec:
    name: str
    module: str
    env: dict[str, str] = field(default_factory=dict)
    python: str = field(default_factory=lambda: sys.executable)

    @property
    def command(self) -> str:
        return self.python

    @property
    def args(self) -> list[str]:
        return ["-m", self.module]

    def stdio_entry(self, *, typed: bool = False) -> dict:
        entry: dict = {}
        if typed:
            entry["type"] = "stdio"
        entry["command"] = self.command
        entry["args"] = self.args
        if self.env:
            entry["env"] = dict(self.env)
        return entry


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _appdata() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")
    if system == "Darwin":
        return _home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")


def _xdg_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")


# --------------------------------------------------------------------------
# File helpers
# --------------------------------------------------------------------------

class ConfigError(Exception):
    """The client config exists but cannot be edited safely."""


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{path} is not plain JSON ({exc}); edit it manually") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} top level is not a JSON object")
    return data


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".pre-mcp-install.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    path.write_text(text, encoding="utf-8", newline="\n")
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    break
                time.sleep(0.05 * (2 ** attempt))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Client adapters
# --------------------------------------------------------------------------

@dataclass
class Client:
    key: str
    label: str
    path: Callable[[], Path]
    detect: Callable[[], bool]
    kind: str  # json | vscode | opencode | codex | claude-code

    def config_path(self) -> Path:
        return self.path()


def _json_client(key: str, label: str, path: Callable[[], Path],
                 detect: Callable[[], bool] | None = None, kind: str = "json") -> Client:
    return Client(key, label, path, detect or (lambda: path().parent.is_dir()), kind)


CLIENTS: list[Client] = [
    Client("claude-code", "Claude Code", lambda: _home() / ".claude.json",
           lambda: bool(shutil.which("claude")) or (_home() / ".claude.json").exists(), "claude-code"),
    _json_client("claude-desktop", "Claude Desktop",
                 lambda: _appdata() / "Claude" / "claude_desktop_config.json"),
    _json_client("cursor", "Cursor", lambda: _home() / ".cursor" / "mcp.json"),
    _json_client("windsurf", "Windsurf", lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json"),
    _json_client("gemini-cli", "Gemini CLI", lambda: _home() / ".gemini" / "settings.json",
                 lambda: bool(shutil.which("gemini")) or (_home() / ".gemini" / "settings.json").exists()),
    _json_client("antigravity", "Antigravity", lambda: _home() / ".gemini" / "config" / "mcp_config.json",
                 lambda: (_home() / ".gemini" / "config").is_dir()
                 or (_home() / ".gemini" / "antigravity-cli").is_dir()),
    _json_client("antigravity-ide", "Antigravity IDE",
                 lambda: _home() / ".gemini" / "antigravity" / "mcp_config.json"),
    Client("codex", "Codex CLI", lambda: _home() / ".codex" / "config.toml",
           lambda: (_home() / ".codex").is_dir() or bool(shutil.which("codex")), "codex"),
    Client("opencode", "OpenCode", lambda: _xdg_config() / "opencode" / "opencode.json",
           lambda: (_xdg_config() / "opencode").is_dir() or bool(shutil.which("opencode")), "opencode"),
    Client("vscode", "VS Code (Copilot)", lambda: _appdata() / "Code" / "User" / "mcp.json",
           lambda: (_appdata() / "Code" / "User").is_dir(), "vscode"),
    _json_client("cline", "Cline", lambda: _appdata() / "Code" / "User" / "globalStorage"
                 / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"),
]

CLIENT_KEYS = [c.key for c in CLIENTS]


def get_client(key: str) -> Client:
    for client in CLIENTS:
        if client.key == key:
            return client
    raise KeyError(f"unknown client {key!r}; choose from: {', '.join(CLIENT_KEYS)}")


def detected_clients() -> list[Client]:
    found = []
    for client in CLIENTS:
        try:
            if client.detect():
                found.append(client)
        except Exception:
            continue
    return found


# -- per-format edits -------------------------------------------------------

def _json_key(kind: str) -> str:
    return "servers" if kind == "vscode" else ("mcp" if kind == "opencode" else "mcpServers")


def _json_entry(kind: str, spec: ServerSpec) -> dict:
    if kind == "opencode":
        entry: dict = {"type": "local", "command": [spec.command, *spec.args], "enabled": True}
        if spec.env:
            entry["environment"] = dict(spec.env)
        return entry
    return spec.stdio_entry(typed=kind in ("vscode", "claude-code"))


def _edit_json(client: Client, spec: ServerSpec, remove: bool, dry_run: bool) -> str:
    path = client.config_path()
    data = _read_json(path)
    key = _json_key(client.kind)
    servers = data.get(key)
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise ConfigError(f"{path}: '{key}' is not an object")
    if remove:
        if spec.name not in servers:
            return "absent"
        servers.pop(spec.name)
        status = "removed"
    else:
        entry = _json_entry(client.kind, spec)
        if servers.get(spec.name) == entry:
            return "unchanged"
        status = "updated" if spec.name in servers else "added"
        servers[spec.name] = entry
    data[key] = servers
    if client.kind == "opencode" and not path.exists():
        data = {"$schema": "https://opencode.ai/config.json", **data}
    if not dry_run:
        _write_json(path, data)
    return status


_TOML_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _toml_str(value: str) -> str:
    # JSON string escapes are a valid TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def _strip_toml_table(text: str, table: str) -> tuple[str, bool]:
    out: list[str] = []
    skipping = False
    found = False
    for line in text.splitlines(keepends=True):
        match = _TOML_HEADER.match(line)
        if match:
            name = match.group(1).replace(" ", "").replace('"', "")
            skipping = name == table or name.startswith(table + ".")
            found = found or skipping
        if not skipping:
            out.append(line)
    return "".join(out), found


def _edit_codex(client: Client, spec: ServerSpec, remove: bool, dry_run: bool) -> str:
    path = client.config_path()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    table = f"mcp_servers.{spec.name}"
    stripped, found = _strip_toml_table(original, table)
    if remove:
        if not found:
            return "absent"
        new_text, status = stripped, "removed"
    else:
        block = [f"[{table}]", f"command = {_toml_str(spec.command)}",
                 "args = [" + ", ".join(_toml_str(a) for a in spec.args) + "]"]
        if spec.env:
            block.append(f"\n[{table}.env]")
            block.extend(f"{k} = {_toml_str(v)}" for k, v in spec.env.items())
        body = stripped.rstrip("\n")
        new_text = (body + "\n\n" if body else "") + "\n".join(block) + "\n"
        if new_text == original:
            return "unchanged"
        status = "updated" if found else "added"
    if _tomllib is not None:
        try:
            _tomllib.loads(new_text)
        except Exception as exc:
            raise ConfigError(f"{path}: refusing to write invalid TOML ({exc})") from exc
    if not dry_run:
        _atomic_write(path, new_text)
    return status


def _edit_claude_code(client: Client, spec: ServerSpec, remove: bool, dry_run: bool) -> str:
    exe = shutil.which("claude")
    if not exe:
        return _edit_json(client, spec, remove, dry_run)
    listed = subprocess.run([exe, "mcp", "get", spec.name], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    present = listed.returncode == 0
    if dry_run:
        return ("removed" if present else "absent") if remove else ("updated" if present else "added")
    if present:
        subprocess.run([exe, "mcp", "remove", "--scope", "user", spec.name],
                       capture_output=True, text=True, timeout=60)
    if remove:
        return "removed" if present else "absent"
    payload = json.dumps(_json_entry("claude-code", spec), ensure_ascii=False)
    added = subprocess.run([exe, "mcp", "add-json", "--scope", "user", spec.name, payload],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if added.returncode != 0:
        raise ConfigError(f"claude mcp add-json failed: {(added.stderr or added.stdout).strip()}")
    return "updated" if present else "added"


_EDITORS = {
    "json": _edit_json,
    "vscode": _edit_json,
    "opencode": _edit_json,
    "codex": _edit_codex,
    "claude-code": _edit_claude_code,
}


def apply(spec: ServerSpec, clients: list[Client], *, remove: bool = False,
          dry_run: bool = False) -> list[tuple[Client, str, str]]:
    """Install/remove ``spec`` in each client. Never raises per client."""
    results = []
    for client in clients:
        try:
            status = _EDITORS[client.kind](client, spec, remove, dry_run)
            detail = str(client.config_path())
        except ConfigError as exc:
            status, detail = "skipped", str(exc)
        except Exception as exc:  # one broken client must not stop the rest
            status, detail = "error", f"{type(exc).__name__}: {exc}"
        results.append((client, status, detail))
    return results


def registered_in(spec: ServerSpec, client: Client) -> bool | None:
    """True/False when determinable, None when the config cannot be read."""
    try:
        path = client.config_path()
        if client.kind == "codex":
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            return _strip_toml_table(text, f"mcp_servers.{spec.name}")[1]
        if client.kind == "claude-code" and shutil.which("claude"):
            return subprocess.run([shutil.which("claude"), "mcp", "get", spec.name],
                                  capture_output=True, timeout=60).returncode == 0
        servers = _read_json(path).get(_json_key(client.kind)) or {}
        return isinstance(servers, dict) and spec.name in servers
    except Exception:
        return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def generic_snippet(spec: ServerSpec) -> str:
    return json.dumps({"mcpServers": {spec.name: spec.stdio_entry()}}, indent=2, ensure_ascii=False)


def _select(args: argparse.Namespace) -> list[Client]:
    if getattr(args, "client", None):
        return [get_client(k) for k in args.client]
    return detected_clients()


def _report(results: list[tuple[Client, str, str]], out) -> int:
    if not results:
        out("No supported agent clients detected. Use --client <name> or --print for a manual snippet.")
        return 1
    width = max(len(c.label) for c, _, _ in results)
    failures = 0
    for client, status, detail in results:
        out(f"  {client.label.ljust(width)}  {status:<9}  {detail}")
        failures += status in ("error",)
    return 1 if failures else 0


def add_install_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--client", action="append", choices=CLIENT_KEYS,
                        help="target client (repeatable); default: every detected client")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print a generic mcpServers JSON snippet and exit")


def run_install(spec: ServerSpec, args: argparse.Namespace, *, remove: bool = False, out=print) -> int:
    if getattr(args, "print_only", False):
        out(generic_snippet(spec))
        return 0
    clients = _select(args)
    verb = "Removing" if remove else "Registering"
    suffix = " (dry run)" if getattr(args, "dry_run", False) else ""
    out(f"{verb} MCP server '{spec.name}'{suffix}: {spec.command} {' '.join(spec.args)}")
    code = _report(apply(spec, clients, remove=remove, dry_run=getattr(args, "dry_run", False)), out)
    if not remove and code == 0 and clients:
        out("Restart the agent client(s) to load the server.")
    return code


def run_clients(spec: ServerSpec, out=print) -> int:
    detected = {c.key for c in detected_clients()}
    width = max(len(c.label) for c in CLIENTS)
    for client in CLIENTS:
        found = client.key in detected
        reg = registered_in(spec, client) if found else None
        mark = "registered" if reg else ("not registered" if found else "not detected")
        out(f"  {client.key:<16} {client.label.ljust(width)}  {mark:<15} {client.config_path()}")
    return 0
