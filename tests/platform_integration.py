"""Real 1C test. Creates only a new disposable base below this project's state directory.

Never modifies DemoTrd. Keeps the resulting base, logs and CF for inspection.
"""
import json
import subprocess
import time
from pathlib import Path

from onecodex.core import Bridge, write_json


root = Path(__file__).resolve().parents[1]
test_root = root / "state" / "platform-validation"
base = test_root / ("base-" + str(time.time_ns()))
base.parent.mkdir(parents=True, exist_ok=True)
assert not base.exists() and base.resolve().is_relative_to((root / "state").resolve())
platform = r"C:\Program Files\1cv8\8.3.27.1859\bin\1cv8.exe"
log = test_root / "create.log"
print(f"Creating isolated test base: {base}", flush=True)
created = subprocess.run([platform, "CREATEINFOBASE", f'File={base};',
    "/DisableStartupDialogs", "/DisableStartupMessages", "/Out", str(log)],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
if created.returncode != 0:
    raise SystemExit(f"CREATEINFOBASE failed: {created.returncode}, {log}")
config = test_root / "connections.json"
write_json(config, {"platform": platform, "state_dir": "bridge-state", "connections": {
    "validation": {"kind": "file", "location": str(base), "allow_apply": True, "timeout_sec": 180}}})
bridge = Bridge(config)
try:
    # Seed only a root module with a comment. The platform supplies the configuration itself.
    seed = test_root / "seed" / "Ext" / "ManagedApplicationModule.bsl"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text("// OneCodex integration baseline\n", encoding="utf-8-sig")
    bridge.run("validation", ["/LoadConfigFromFiles", str(seed.parent.parent),
        "-Format", "Hierarchical", "-partial", "-files", str(seed)], "seed")
    snapshot = bridge._sync("validation")
    path = "Ext/ManagedApplicationModule.bsl"
    module = bridge.read_module("validation", path)
    bridge.stage_module("validation", path, "// OneCodex integration changed\n", module["sha256"])
    plan = bridge.review_changes("validation")
    result = bridge._apply("validation", plan["plan_id"])
    after = bridge.read_module("validation", path)
    assert result["loaded"] and not result["database_updated"]
    assert "integration changed" in after["text"]
    assert Path(result["backup"]).exists()
    repeated = bridge._sync("validation")
    assert repeated["unchanged"]
    report = {"passed": True, "platform": "8.3.27.1859", "test_base": str(base),
              "initial_snapshot": snapshot, "apply_result": result,
              "after": after, "unchanged_sync": repeated}
    write_json(test_root / "result.json", report)
    print(json.dumps({"passed": True, "backup": result["backup"], "test_base": str(base)}, ensure_ascii=False), flush=True)
finally:
    bridge.close()
