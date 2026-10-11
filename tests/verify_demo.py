"""Read real DemoTrd metadata/code and test a local draft without importing it."""
import json
import sys
from pathlib import Path

from onecodex.core import Bridge, write_json


sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).resolve().parents[1]
bridge = Bridge(root / "connections.json")
try:
    connection = "demotrd"
    assert bridge.profile(connection)["allow_apply"] is False
    if bridge.review_changes(connection)["changes"]:
        raise SystemExit("Existing user draft found; verification stopped without changing it.")
    found = bridge.search_metadata(connection, "РеализацияТоваровУслуг", "Document")
    assert found["total"] == 1
    metadata = bridge.get_metadata(connection, found["objects"][0]["address"])
    path = next(p for p in metadata["modules"] if p.endswith("/ObjectModule.bsl"))
    module = bridge.read_module(connection, path, 1, 5)
    old = "#Область ПрограммныйИнтерфейс"
    bridge.patch_module(connection, path, old, "// OneCodex: локальная проверка черновика\n" + old, module["sha256"])
    reviewed = bridge.review_changes(connection)
    assert len(reviewed["changes"]) == 1
    assert "локальная проверка черновика" in reviewed["changes"][0]["diff"]
    assert bridge.read_module(connection, path)["sha256"] == module["sha256"]
    bridge.discard_changes(connection)
    report = {"passed": True, "database_modified": False,
              "metadata_address": metadata["address"], "metadata_child_kinds": list(metadata["children"]),
              "associated_modules": len(metadata["modules"]), "module_path": path,
              "module_lines": module["total_lines"], "module_sha256": module["sha256"],
              "local_draft_reviewed_and_discarded": True}
    write_json(root / "state" / "demo-validation.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    bridge.close()
