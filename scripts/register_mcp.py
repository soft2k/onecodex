"""Append only the onecodex server table; preserve all existing user settings."""
import json
import sys
import time
import tomllib
from pathlib import Path


root = Path(__file__).resolve().parents[1]
target = Path.home() / ".codex" / "config.toml"
data = target.read_text(encoding="utf-8-sig") if target.exists() else ""
parsed = tomllib.loads(data)
expected = {"command": str(root / ".venv" / "Scripts" / "python.exe"),
            "args": ["-m", "onecodex", "--config", str(root / "connections.json"), "serve"],
            "cwd": str(root), "startup_timeout_sec": 20, "tool_timeout_sec": 60}
existing = parsed.get("mcp_servers", {}).get("onecodex")
if existing:
    if all(existing.get(key) == value for key, value in expected.items()):
        print("onecodex already registered")
        sys.exit(0)
    raise SystemExit("An existing onecodex registration differs; nothing was changed.")
fragment = "\n\n[mcp_servers.onecodex]\n" + "\n".join(
    f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in expected.items()) + "\n"
merged = data.rstrip() + fragment
tomllib.loads(merged)
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists():
    backup = target.with_name(f"config.toml.before-onecodex-{time.time_ns()}.bak")
    backup.write_text(data, encoding="utf-8")
    print(f"Backup: {backup}")
target.write_text(merged, encoding="utf-8")
(root / "codex-mcp.toml").write_text(fragment.strip() + "\n", encoding="utf-8")
print(f"Registered onecodex in {target}")
