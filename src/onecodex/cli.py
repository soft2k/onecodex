from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .core import Bridge, BridgeError, write_json


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def wait(bridge, receipt):
    while True:
        job = bridge.get_job(receipt["job_id"])
        if job["status"] in {"done", "failed"}:
            output(job)
            return 0 if job["status"] == "done" else 1
        time.sleep(0.25)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="OneCodex: локальный мост к конфигуратору 1С")
    parser.add_argument("--config", default="connections.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Запустить MCP stdio-сервер")
    sub.add_parser("connections", help="Показать подключения")
    sub.add_parser("discover", help="Найти базы из списка запуска 1С")
    p = sub.add_parser("import-base", help="Добавить найденную базу")
    p.add_argument("base_id")
    p.add_argument("--user", default="")
    p.add_argument("--password-env", default="")
    p.add_argument("--platform", default="")
    sub.add_parser("codex-config", help="Сформировать фрагмент MCP config.toml")
    for name in ("sync", "review", "check", "discard"):
        p = sub.add_parser(name)
        p.add_argument("connection")
    p = sub.add_parser("metadata")
    p.add_argument("connection")
    p.add_argument("--text", default="")
    p.add_argument("--address")
    p = sub.add_parser("modules")
    p.add_argument("connection")
    p.add_argument("--text", default="")
    p = sub.add_parser("read")
    p.add_argument("connection")
    p.add_argument("path")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("search")
    p.add_argument("connection")
    p.add_argument("text")
    p = sub.add_parser("stage")
    p.add_argument("connection")
    p.add_argument("path")
    p.add_argument("text_file", help="Файл с полным новым текстом модуля")
    p.add_argument("--sha256", required=True)
    p = sub.add_parser("apply")
    p.add_argument("connection")
    p.add_argument("plan_id")
    p = sub.add_parser("add")
    p.add_argument("id")
    p.add_argument("--kind", choices=["file", "server"], required=True)
    p.add_argument("--location", required=True)
    p.add_argument("--user", default="")
    p.add_argument("--password-env")
    p.add_argument("--extension")
    p.add_argument("--platform")
    p.add_argument("--allow-apply", action="store_true")
    args = parser.parse_args()
    bridge = None
    try:
        bridge = Bridge(args.config)
        cmd = args.command
        if cmd == "serve":
            from .server import make_server
            make_server(bridge).run(transport="stdio")
        elif cmd == "codex-config":
            # JSON strings are valid TOML basic strings; no shell interpolation involved.
            print("[mcp_servers.onecodex]")
            print("command = " + json.dumps(sys.executable))
            print("args = " + json.dumps(["-m", "onecodex", "--config", str(bridge.config_path), "serve"]))
            print("cwd = " + json.dumps(str(bridge.config_path.parent)))
            print("startup_timeout_sec = 20\ntool_timeout_sec = 60")
        elif cmd == "connections":
            output(bridge.list_connections())
        elif cmd == "discover":
            output(bridge.discover_bases())
        elif cmd == "import-base":
            output(bridge.import_base(args.base_id, args.user, args.password_env, args.platform))
        elif cmd in {"sync", "check", "apply"}:
            receipt = {"sync": lambda: bridge.sync_sources(args.connection),
                       "check": lambda: bridge.check_configuration(args.connection),
                       "apply": lambda: bridge.apply_changes(args.connection, args.plan_id)}[cmd]()
            return wait(bridge, receipt)
        elif cmd == "metadata":
            output(bridge.get_metadata(args.connection, args.address) if args.address
                   else bridge.search_metadata(args.connection, args.text))
        elif cmd == "modules":
            output(bridge.list_modules(args.connection, args.text))
        elif cmd == "read":
            output(bridge.read_module(args.connection, args.path, args.start, args.limit))
        elif cmd == "search":
            output(bridge.search_code(args.connection, args.text))
        elif cmd == "stage":
            output(bridge.stage_module(args.connection, args.path,
                   Path(args.text_file).read_text(encoding="utf-8-sig"), args.sha256))
        elif cmd == "review":
            output(bridge.review_changes(args.connection))
        elif cmd == "discard":
            output(bridge.discard_changes(args.connection))
        elif cmd == "add":
            output(bridge.add_connection(args.id, args.kind, args.location, args.user,
                args.password_env or "", args.platform or "", args.extension or "", allow_apply=args.allow_apply))
        return 0
    except (BridgeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if bridge:
            bridge.close()
