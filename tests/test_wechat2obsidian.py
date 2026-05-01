import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wechat2obsidian.py"
SPEC = importlib.util.spec_from_file_location("wechat2obsidian", SCRIPT)
wechat2obsidian = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(wechat2obsidian)


class WeChatToObsidianTests(unittest.TestCase):
    def test_share_card_renders_markdown_link(self):
        raw = b"""
        <msg><appmsg>
          <title>Great Article</title>
          <des>Useful notes</des>
          <url>https://example.com/a</url>
          <sourcedisplayname>Example</sourcedisplayname>
        </appmsg></msg>
        """
        rendered = wechat2obsidian.format_message(49, raw)
        self.assertIn("**[Great Article](https://example.com/a)**", rendered)
        self.assertIn("Useful notes", rendered)

    def test_vault_path_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            with self.assertRaises(SystemExit):
                wechat2obsidian.safe_vault_path(vault, "..", "escape")

    def test_export_chat_writes_daily_markdown_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "message_0.decrypted.db"
            vault = root / "vault"
            vault.mkdir()

            target = "filehelper"
            table = "Msg_" + hashlib.md5(target.encode()).hexdigest()
            timestamp = int(dt.datetime(2026, 1, 2, 9, 30, 0).timestamp())

            con = sqlite3.connect(db)
            con.execute("CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)")
            con.execute("INSERT INTO Name2Id (user_name, is_session) VALUES (?, ?)", (target, 1))
            con.execute(
                f"CREATE TABLE {table} ("
                "local_id INTEGER, local_type INTEGER, create_time INTEGER, "
                "message_content BLOB, real_sender_id INTEGER)"
            )
            con.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",
                (1, 1, timestamp, b"hello obsidian", 1),
            )
            con.commit()
            con.close()

            code = wechat2obsidian.main([
                "export-chat",
                "--db",
                str(db),
                "--target",
                target,
                "--vault",
                str(vault),
                "--folder",
                "WeChat",
                "--no-attachments",
                "--with-senders",
            ])
            self.assertEqual(code, 0)

            md = vault / "WeChat" / target / "2026-01" / "2026-01-02.md"
            self.assertTrue(md.exists())
            text = md.read_text(encoding="utf-8")
            self.assertIn("hello obsidian", text)
            self.assertIn("`sender`: filehelper", text)

            manifest = json.loads((vault / "WeChat" / target / "_export_manifest.json").read_text())
            self.assertEqual(manifest["message_count"], 1)
            self.assertEqual(manifest["day_files_written"], 1)

    def test_import_weflow_json_writes_daily_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            media = root / "photo.jpg"
            media.write_bytes(b"fake-jpeg")

            payload = {
                "session": {"displayName": "文件传输助手"},
                "messages": [
                    {
                        "localId": 7,
                        "createTime": int(dt.datetime(2026, 2, 3, 8, 15).timestamp()),
                        "type": "图片消息",
                        "senderDisplayName": "me",
                        "content": "看这个图",
                        "mediaLocalPath": str(media),
                        "mediaType": "image",
                    }
                ],
            }
            input_json = root / "weflow.json"
            input_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            code = wechat2obsidian.main([
                "import-weflow-json",
                "--input",
                str(input_json),
                "--vault",
                str(vault),
                "--folder",
                "WeChat",
            ])
            self.assertEqual(code, 0)

            md = vault / "WeChat" / "文件传输助手" / "2026-02" / "2026-02-03.md"
            self.assertTrue(md.exists())
            text = md.read_text(encoding="utf-8")
            self.assertIn("看这个图", text)
            self.assertIn("attachments/photo.jpg", text)
            self.assertTrue((md.parent / "attachments" / "photo.jpg").exists())


if __name__ == "__main__":
    unittest.main()
