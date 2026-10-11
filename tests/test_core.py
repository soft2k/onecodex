import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from onecodex.core import Bridge, BridgeError, parse_metadata, safe_path, sha, write_json


MODULE = "CommonModules/Example/Ext/Module.bsl"
OLD = b"\xef\xbb\xbf" + "Функция Значение() Экспорт\r\n\tВозврат 1;\r\nКонецФункции\r\n".encode()
NEW = "Функция Значение() Экспорт\n\tВозврат 2;\nКонецФункции\n"
XML = '''<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
<Catalog uuid="11111111-1111-1111-1111-111111111111"><Properties><Name>Товары</Name>
<Synonym><item><lang>ru</lang><content>Номенклатура</content></item></Synonym></Properties>
<ChildObjects><Attribute><Properties><Name>Артикул</Name><Type><Type>xs:string</Type></Type></Properties></Attribute>
<Attribute><Properties><Name>Вес</Name></Properties></Attribute>
<TabularSection><Properties><Name>Цены</Name></Properties></TabularSection></ChildObjects></Catalog></MetaDataObject>'''


class FakeBridge(Bridge):
    """A fake Designer for testing database-write ordering and conflicts."""
    def __init__(self, config):
        super().__init__(config)
        self.remote_generation = "a" * 40
        self.remote_module = OLD
        self.operations = []
        self.fail_export = False
        self.fail_check = False

    def generation(self, connection, job=None):
        return self.remote_generation

    def run(self, connection, operation, label, job=None):
        self.operations.append(operation)
        if operation[0] == "/DumpConfigToFiles":
            if self.fail_export:
                raise BridgeError("export failure")
            root = Path(operation[1])
            root.mkdir(parents=True, exist_ok=True)
            (root / "Configuration.xml").write_text(XML.replace("Catalog", "Configuration"), encoding="utf-8")
            (root / "ConfigDumpInfo.xml").write_text("<ConfigDumpInfo/>")
            obj = root / "Catalogs/Товары.xml"
            obj.parent.mkdir(parents=True, exist_ok=True)
            obj.write_text(XML, encoding="utf-8")
            module = safe_path(root, MODULE)
            module.parent.mkdir(parents=True, exist_ok=True)
            module.write_bytes(self.remote_module)
        elif operation[0] == "/DumpCfg":
            Path(operation[1]).write_bytes(b"fake-cf")
        elif operation[0] == "/LoadConfigFromFiles":
            self.remote_module = safe_path(Path(operation[1]), MODULE).read_bytes()
            self.remote_generation = "b" * 40
        elif operation[0] == "/CheckConfig" and self.fail_check:
            raise BridgeError("syntax error")
        return {"exit_code": 0, "log": "fake.log", "output": ""}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "connections.json"
        write_json(self.config, {"platform": __file__, "connections": {
            "test": {"kind": "file", "location": str(self.root / "database"), "allow_apply": True},
            "server": {"kind": "server", "location": "srv\\db", "user": "Admin", "password_env": "OC_TEST_SECRET"}}})
        self.bridge = FakeBridge(self.config)
        self.bridge._sync("test")

    def tearDown(self):
        self.bridge.close()
        self.temp.cleanup()

    def stage(self):
        return self.bridge.stage_module("test", MODULE, NEW, sha(OLD))

    def test_metadata_types_and_repeated_attributes(self):
        obj = self.bridge.get_metadata("test", "Catalog.Товары")
        self.assertEqual(obj["children"]["Attribute"][0]["Properties"]["Type"]["Type"], "xs:string")
        self.assertEqual(len(obj["children"]["Attribute"]), 2)
        self.assertEqual(self.bridge.search_metadata("test", "номенклатура")["total"], 2)

    def test_subordinate_addresses_are_qualified_by_parent(self):
        folder = self.bridge.directory("test")
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        index["objects"] += [
            {"address": "Document.Продажа", "kind": "Document", "name": "Продажа", "uuid": "a",
             "path": "Documents/Продажа.xml", "synonym": ""},
            {"address": "Form.ФормаОбъекта", "kind": "Form", "name": "ФормаОбъекта", "uuid": "b",
             "path": "Documents/Продажа/Forms/ФормаОбъекта.xml", "synonym": ""}]
        write_json(folder / "index.json", index)
        result = self.bridge.search_metadata("test", "", "Form")
        self.assertEqual(result["objects"][0]["address"], "Document.Продажа.Form.ФормаОбъекта")

    def test_module_read_pagination_and_search_line_numbers(self):
        result = self.bridge.read_module("test", MODULE, 2, 1)
        self.assertEqual(result["sha256"], sha(OLD))
        self.assertTrue(result["has_more"])
        self.assertEqual(self.bridge.search_code("test", "возврат")["hits"][0]["line"], 2)

    def test_path_escape_is_rejected(self):
        for path in ("../secret.bsl", "C:/secret.bsl", "/secret.bsl", "\\\\server\\share", "x\n.bsl"):
            with self.subTest(path=path), self.assertRaises(BridgeError):
                safe_path(self.root, path)

    def test_staging_preserves_original_and_bom_crlf(self):
        self.stage()
        self.assertEqual(safe_path(self.bridge.sources("test"), MODULE).read_bytes(), OLD)
        draft = safe_path(self.bridge.directory("test") / "draft", MODULE).read_bytes()
        self.assertTrue(draft.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", draft)
        self.assertEqual(draft.decode("utf-8-sig"), NEW.replace("\n", "\r\n"))

    def test_stale_source_hash_rejected(self):
        with self.assertRaises(BridgeError):
            self.bridge.stage_module("test", MODULE, NEW, "0" * 64)

    def test_exact_patch_preserves_rest_of_large_module(self):
        self.bridge.patch_module("test", MODULE, "Возврат 1;", "Возврат 2;", sha(OLD))
        after = safe_path(self.bridge.directory("test") / "draft", MODULE).read_bytes()
        self.assertEqual(after, OLD.replace("Возврат 1;".encode(), "Возврат 2;".encode()))
        with self.assertRaises(BridgeError):
            self.bridge.patch_module("test", MODULE, "not found", "new", sha(OLD))

    def test_sync_does_not_overwrite_draft(self):
        self.stage()
        path = safe_path(self.bridge.directory("test") / "draft", MODULE)
        before = path.read_bytes()
        self.bridge.remote_generation = "c" * 40
        self.bridge.remote_module = b"// external change"
        self.bridge._sync("test")
        self.assertEqual(path.read_bytes(), before)

    def test_unchanged_configuration_skips_export(self):
        self.bridge.operations.clear()
        self.assertTrue(self.bridge._sync("test")["unchanged"])
        self.assertEqual(self.bridge.operations, [])

    def test_changed_configuration_uses_incremental_dump(self):
        self.bridge.operations.clear()
        self.bridge.remote_generation = "c" * 40
        self.bridge._sync("test")
        self.assertIn("-update", self.bridge.operations[0])

    def test_failed_export_invalidates_snapshot(self):
        self.bridge.remote_generation = "c" * 40
        self.bridge.fail_export = True
        with self.assertRaises(BridgeError):
            self.bridge._sync("test")
        with self.assertRaises(BridgeError):
            self.bridge.list_modules("test")

    def test_successful_apply_is_selective_and_backed_up(self):
        self.stage()
        plan = self.bridge.review_changes("test")
        self.assertIn("Возврат 2", plan["changes"][0]["diff"])
        result = self.bridge._apply("test", plan["plan_id"])
        self.assertTrue(result["loaded"])
        self.assertFalse(result["database_updated"])
        self.assertTrue(Path(result["backup"]).exists())
        load = next(o for o in self.bridge.operations if o[0] == "/LoadConfigFromFiles")
        self.assertIn("-partial", load)
        listed = Path(load[load.index("-listFile") + 1]).read_text(encoding="utf-8").strip()
        self.assertEqual(listed.replace("\\", "/"), MODULE)
        self.assertFalse(any("/UpdateDBCfg" in o for o in self.bridge.operations))
        self.assertEqual(self.bridge.review_changes("test")["changes"], [])

    def test_remote_conflict_blocks_load_and_keeps_draft(self):
        self.stage()
        plan = self.bridge.review_changes("test")
        self.bridge.remote_generation = "c" * 40
        with self.assertRaisesRegex(BridgeError, "База изменилась"):
            self.bridge._apply("test", plan["plan_id"])
        self.assertFalse(any(o[0] == "/LoadConfigFromFiles" for o in self.bridge.operations))
        self.assertEqual(len(self.bridge.review_changes("test")["changes"]), 1)

    def test_old_review_plan_cannot_apply_new_draft(self):
        self.stage()
        plan = self.bridge.review_changes("test")
        self.bridge.stage_module("test", MODULE, NEW.replace("2", "3"), sha(OLD))
        with self.assertRaisesRegex(BridgeError, "План устарел"):
            self.bridge._apply("test", plan["plan_id"])

    def test_external_draft_edit_is_detected(self):
        self.stage()
        plan = self.bridge.review_changes("test")
        safe_path(self.bridge.directory("test") / "draft", MODULE).write_bytes(b"// tamper")
        with self.assertRaises(BridgeError):
            self.bridge._apply("test", plan["plan_id"])

    def test_check_failure_does_not_claim_rollback(self):
        self.stage()
        plan = self.bridge.review_changes("test")
        self.bridge.fail_check = True
        result = self.bridge._apply("test", plan["plan_id"])
        self.assertTrue(result["loaded"])
        self.assertFalse(result["check"]["passed"])
        self.assertIn("Возврат 2", self.bridge.remote_module.decode("utf-8-sig"))

    def test_read_only_profile_blocks_apply(self):
        self.bridge.config["connections"]["test"]["allow_apply"] = False
        with self.assertRaisesRegex(BridgeError, "выключена"):
            self.bridge.apply_changes("test", "a" * 32)

    def test_command_server_credentials_redacted(self):
        with patch.dict(os.environ, {"OC_TEST_SECRET": "test-secret"}):
            args = self.bridge.command("server", ["/GetConfigGenerationID"], self.root / "log")
        self.assertIn("/S", args)
        self.assertIn("srv\\db", args)
        self.assertNotIn("test-secret", args)
        self.assertIn("<hidden>", args)

    def test_cross_instance_lock_prevents_parallel_operations(self):
        with self.bridge.locked("test"), self.assertRaises(BridgeError):
            with self.bridge.locked("test"):
                pass

    def test_xml_dtd_is_rejected(self):
        path = self.root / "bad.xml"
        path.write_text('<!DOCTYPE x [<!ENTITY y "hello">]><x/>')
        with self.assertRaises(BridgeError):
            parse_metadata(path)

    def test_background_receipt_completes(self):
        receipt = self.bridge.sync_sources("test")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.bridge.get_job(receipt["job_id"])
            if job["status"] in {"done", "failed"}:
                break
            time.sleep(.01)
        self.assertEqual(job["status"], "done")


if __name__ == "__main__":
    unittest.main()
