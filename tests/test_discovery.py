import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onecodex.core import Bridge, BridgeError, write_json
from onecodex.discovery import base_id, connection_fields, discover, parse_v8i, read_list_text


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.launcher = self.root / "roaming/1C/1CEStart"
        self.launcher.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"APPDATA": str(self.root / "roaming"),
                                          "PROGRAMDATA": str(self.root / "system")})
        self.env.start()
        self.list = self.launcher / "ibases.v8i"
        self.list.write_text('[Группа]\nFolder=/\n[Тест]\nConnect=File="C:\\Базы\\Тест";\nVersion=8.3\n'
                             '[Сервер]\nConnect=Srvr="srv1c";Ref="test";Usr="Admin";Pwd="secret";\n', encoding="utf-8-sig")
        self.config = self.root / "connections.json"
        write_json(self.config, {"platform": __file__, "connections": {}})
        self.bridge = Bridge(self.config)

    def tearDown(self):
        self.bridge.close()
        self.env.stop()
        self.temp.cleanup()

    def test_file_server_folders_and_no_password_leak(self):
        result = discover()
        self.assertEqual(len(result["bases"]), 2)
        self.assertEqual(result["bases"][1]["location"], "srv1c\\test")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("Admin", json.dumps(result))

    def test_quoted_semicolon_and_doubled_quotes(self):
        fields = connection_fields('File="C:\\folder;test";Usr="user""name";')
        self.assertEqual(fields["file"], "C:\\folder;test")
        self.assertEqual(fields["usr"], 'user"name')

    def test_bad_records_and_web_are_reported_without_secrets(self):
        values, warnings = parse_v8i('[bad]\nConnect=File="oops;Pwd="secret";\n'
            '[web]\nConnect=WS="https://user:secret@host/base";\n', self.list)
        self.assertEqual(values, [])
        self.assertEqual(len(warnings), 2)
        self.assertNotIn("secret", json.dumps(warnings))

    def test_shared_lists_and_duplicate_aliases(self):
        shared = self.root / "common.v8i"
        shared.write_text('[Альтернативное имя]\nConnect=File="c:\\базы\\тест\\";\n', encoding="utf-8")
        (self.launcher / "1cestart.cfg").write_text(f"CommonInfoBases={shared}\n", encoding="utf-8")
        result = discover()
        self.assertEqual(len(result["bases"]), 2)
        self.assertEqual(len(result["bases"][0]["aliases"]), 2)
        self.assertEqual(len(result["bases"][0]["sources"]), 2)

    def test_import_preserves_existing_profile(self):
        bid = base_id("file", r"C:\Базы\Тест")
        self.bridge.add_connection("existing", "file", r"C:\Базы\Тест", "Admin", allow_apply=True)
        result = self.bridge.import_base(bid, user="Other")
        self.assertEqual(result["connection"], "existing")
        self.assertEqual(self.bridge.profile("existing")["user"], "Admin")
        self.assertTrue(self.bridge.profile("existing")["allow_apply"])

    def test_list_then_import_without_export(self):
        listed = self.bridge.list_connections()
        self.assertEqual(len(listed), 2)
        self.assertFalse(listed[0]["configured"])
        added = self.bridge.import_base(listed[0]["base_id"], user="Developer")
        self.assertEqual(added["profile"]["user"], "Developer")
        self.assertFalse(added["profile"]["allow_apply"])
        self.assertEqual(added["profile"]["required_version"], "8.3")
        self.assertTrue(self.bridge.list_connections()[0]["configured"])
        self.assertFalse(self.bridge.sources(added["added"]).exists())

    def test_configuration_name_version_from_existing_snapshot(self):
        self.bridge.add_connection("existing", "file", r"C:\Базы\Тест")
        source = self.bridge.sources("existing")
        source.mkdir(parents=True)
        (source / "Configuration.xml").write_text('<MetaDataObject><Configuration><Properties>'
            '<Name>УправлениеТорговлей</Name><Version>11.5.20.1</Version>'
            '</Properties></Configuration></MetaDataObject>', encoding="utf-8")
        write_json(self.bridge.directory("existing") / "snapshot.json", {"ready": True})
        summary = self.bridge.list_connections()[0]["configuration"]
        self.assertEqual(summary["name"], "УправлениеТорговлей")
        self.assertEqual(summary["version"], "11.5.20.1")

    def test_add_does_not_overwrite_or_lose_external_changes(self):
        self.bridge.add_connection("one", "server", "srv\\one")
        other = Bridge(self.config)
        try:
            other.add_connection("two", "server", "srv\\two")
            self.bridge.add_connection("three", "server", "srv\\three")
        finally:
            other.close()
        with self.assertRaises(BridgeError):
            self.bridge.add_connection("one", "file", "somewhere")
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(set(config["connections"]), {"one", "two", "three"})

    def test_file_encoding_support(self):
        for encoding in ("utf-16", "cp1251", "utf-8-sig"):
            self.list.write_text('[База]\nConnect=File="C:\\Тест";\n', encoding=encoding)
            values, _ = parse_v8i(read_list_text(self.list), self.list)
            self.assertEqual(values[0]["name"], "База")

    def test_version_mismatch_blocks_platform_launch(self):
        self.bridge.add_connection("wrong", "file", "test", required_version="8.5.1")
        executable = self.root / "8.3.27.1859/bin/1cv8.exe"
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.bridge.config["connections"]["wrong"]["platform"] = str(executable)
        with self.assertRaisesRegex(BridgeError, "8.5.1"):
            self.bridge.command("wrong", ["/GetConfigGenerationID"], self.root / "log")


if __name__ == "__main__":
    unittest.main()
