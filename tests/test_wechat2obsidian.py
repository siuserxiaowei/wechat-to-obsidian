import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

    def test_import_wx_cli_json_writes_daily_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            payload = {
                "chat": "文件传输助手",
                "messages": [
                    {
                        "timestamp": int(dt.datetime(2026, 4, 30, 21, 20).timestamp()),
                        "time": "2026-04-30 21:20",
                        "sender": "me",
                        "content": "wx-cli 导入到 Obsidian",
                        "type": "text",
                        "local_id": 11,
                    }
                ],
            }
            input_json = root / "wx-history.json"
            input_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            code = wechat2obsidian.main([
                "import-wx-cli",
                "--input-json",
                str(input_json),
                "--vault",
                str(vault),
                "--folder",
                "WeChat",
            ])
            self.assertEqual(code, 0)

            md = vault / "WeChat" / "文件传输助手" / "2026-04" / "2026-04-30.md"
            self.assertTrue(md.exists())
            text = md.read_text(encoding="utf-8")
            self.assertIn("source: wx-cli", text)
            self.assertIn("wx-cli 导入到 Obsidian", text)
            manifest = json.loads((vault / "WeChat" / "文件传输助手" / "_wx_cli_import_manifest.json").read_text())
            self.assertEqual(manifest["source"], "wx-cli")
            self.assertEqual(manifest["message_count"], 1)

    def test_resolve_wx_session_rejects_ambiguous_names_and_filters_placeholders(self):
        sessions = [
            {
                "chat": "同名测试群",
                "username": "111@chatroom",
                "chat_type": "group",
                "is_group": True,
            },
            {
                "chat": "同名测试群",
                "username": "222@chatroom",
                "chat_type": "group",
                "is_group": True,
            },
            {
                "chat": "@placeholder_foldgroup",
                "username": "@placeholder_foldgroup",
                "chat_type": "folded",
                "is_group": False,
            },
        ]

        with self.assertRaises(SystemExit):
            wechat2obsidian.resolve_wx_session(sessions, chat_name="同名测试群")

        resolved = wechat2obsidian.resolve_wx_session(sessions, chat_id="222@chatroom")
        self.assertEqual(resolved["username"], "222@chatroom")
        self.assertEqual(resolved["display_name"], "同名测试群")

        with self.assertRaises(SystemExit):
            wechat2obsidian.resolve_wx_session(sessions, chat_name="@placeholder_foldgroup")

    def test_import_wx_cli_paginates_dedupes_and_writes_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()

            def fake_run(cmd, capture_output, text, encoding, errors):
                offset = int(cmd[cmd.index("--offset") + 1])
                pages = {
                    0: [
                        {
                            "timestamp": int(dt.datetime(2026, 5, 1, 9, 0).timestamp()),
                            "sender": "Alice",
                            "content": "first",
                            "type": "text",
                            "local_id": 1,
                        },
                        {
                            "timestamp": int(dt.datetime(2026, 5, 1, 9, 1).timestamp()),
                            "from": "Bob",
                            "message": "second",
                            "msg_type": "text",
                            "local_id": 2,
                            "extra_field": "kept for diagnostics",
                        },
                    ],
                    2: [
                        {
                            "timestamp": int(dt.datetime(2026, 5, 1, 9, 1).timestamp()),
                            "sender": "Bob",
                            "content": "second duplicate",
                            "type": "text",
                            "local_id": 2,
                        },
                        {
                            "timestamp": int(dt.datetime(2026, 5, 1, 9, 2).timestamp()),
                            "talker": "Carol",
                            "text": "third",
                            "local_id": 3,
                        },
                    ],
                    4: [
                        {
                            "sender": "NoTime",
                            "content": "dropped because timestamp is missing",
                            "local_id": 4,
                        }
                    ],
                }
                return subprocess.CompletedProcess(cmd, 0, json.dumps(pages[offset], ensure_ascii=False), "")

            with mock.patch.object(wechat2obsidian, "resolve_wx_cli", return_value=("wx", "wx")):
                with mock.patch.object(wechat2obsidian.subprocess, "run", side_effect=fake_run):
                    code = wechat2obsidian.main([
                        "import-wx-cli",
                        "--chat-id",
                        "222@chatroom",
                        "--no-resolve-chat",
                        "--page-size",
                        "2",
                        "--max-messages",
                        "5",
                        "--vault",
                        str(vault),
                        "--folder",
                        "WeChat",
                    ])
            self.assertEqual(code, 0)

            manifest = json.loads((vault / "WeChat" / "222@chatroom" / "_wx_cli_import_manifest.json").read_text())
            self.assertEqual(manifest["pages_fetched"], 3)
            self.assertEqual(manifest["raw_message_count"], 5)
            self.assertEqual(manifest["deduped_count"], 3)
            self.assertEqual(manifest["filtered_count"], 3)
            self.assertEqual(manifest["dropped_count"], 1)
            self.assertEqual(manifest["message_count"], 3)
            self.assertEqual(manifest["resolved_session"]["username"], "222@chatroom")
            self.assertIn("extra_field", manifest["raw_debug"]["unknown_message_keys"])
            self.assertTrue(manifest["warnings"])
            self.assertEqual(manifest["first_message_at"][:10], "2026-05-01")
            self.assertEqual(manifest["last_message_at"][:10], "2026-05-01")

    def test_wx_cli_normalization_keeps_compatible_fields_and_raw_debug(self):
        payload = {
            "chat": "兼容性测试",
            "messages": [
                {
                    "createTime": int(dt.datetime(2026, 5, 2, 10, 0).timestamp()),
                    "from_name": "Sender",
                    "rawContent": "raw body",
                    "localType": 49,
                    "filePath": "/tmp/a.pdf",
                    "unknownFutureField": {"x": 1},
                }
            ],
        }

        title, messages, audit = wechat2obsidian.normalize_wx_cli_payload_with_audit(payload)
        self.assertEqual(title, "兼容性测试")
        self.assertEqual(messages[0]["sender"], "Sender")
        self.assertEqual(messages[0]["content"], "raw body")
        self.assertEqual(messages[0]["media_path"], "/tmp/a.pdf")
        self.assertIn("unknownFutureField", audit["raw_debug"]["unknown_message_keys"])

    def test_multiple_wechat_user_dirs_are_listed_not_auto_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            first = base / "wxid_first"
            second = base / "wxid_second"
            for root in (first, second):
                db_dir = root / "db_storage" / "message"
                db_dir.mkdir(parents=True)
                (db_dir / "message_0.db").write_bytes(b"fake")

            candidates = wechat2obsidian.user_dir_candidates(base)
            self.assertEqual([item["name"] for item in candidates], ["wxid_first", "wxid_second"])
            self.assertTrue(candidates[0]["databases"]["message_0.db"]["exists"])
            with self.assertRaises(SystemExit):
                wechat2obsidian.pick_user_dir(base)


if __name__ == "__main__":
    unittest.main()
