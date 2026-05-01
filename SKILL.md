---
name: wechat-to-obsidian
description: Export WeChat for macOS 4.x local data into an Obsidian vault. Use when a user wants to move WeChat File Transfer Assistant, Favorites, chats, groups, links, notes, files, screenshots, or other saved learning material from WeChat into Obsidian Markdown with attachments. Supports key capture, SQLCipher database decryption, conversation discovery, and daily Markdown export.
---

# WeChat To Obsidian

Use this skill to move data from a user's own WeChat for macOS 4.x account into an Obsidian vault. The bundled CLI is `scripts/wechat2obsidian.py`.

Only operate on the user's own local WeChat data. Do not upload decrypted databases, key logs, attachments, or exported vault contents to third-party services.

## Workflow

1. Check the local environment:

```bash
python3 scripts/wechat2obsidian.py doctor
```

2. Install runtime dependencies if needed:

```bash
python3 -m pip install -r requirements.txt
```

3. Create an ad-hoc signed WeChat copy. Re-run this after major WeChat upgrades:

```bash
python3 scripts/wechat2obsidian.py sign-wechat \
  --dest ~/Desktop/WeChat-Obsidian.app
```

4. Capture SQLCipher keys while opening the target WeChat surfaces. For file transfer exports, open "文件传输助手"; for favorites, open "收藏"; for a group or friend, open that conversation:

```bash
python3 scripts/wechat2obsidian.py capture-keys \
  --wechat-app ~/Desktop/WeChat-Obsidian.app \
  --launch \
  --wait 300
```

Captured logs default to `~/.cache/wechat-to-obsidian/keys.log` and contain only salts and derived encryption keys.

5. Decrypt the message database:

```bash
USER_DIR=$(python3 scripts/wechat2obsidian.py locate-user --print-path)

python3 scripts/wechat2obsidian.py decrypt \
  --db "$USER_DIR/db_storage/message/message_0.db" \
  --out /tmp/message_0.decrypted.db
```

6. Discover exportable chat targets:

```bash
python3 scripts/wechat2obsidian.py list-targets \
  --db /tmp/message_0.decrypted.db \
  --limit 50
```

7. Export to Obsidian:

```bash
python3 scripts/wechat2obsidian.py export-chat \
  --db /tmp/message_0.decrypted.db \
  --target filehelper \
  --vault ~/Documents/Obsidian \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --with-senders
```

The exporter writes one Markdown file per day under `<vault>/<folder>/<subfolder>/<YYYY-MM>/`, copies monthly attachments into `attachments/`, and writes `_export_manifest.json`.

## Common Tasks

- Export File Transfer Assistant: use `--target filehelper`.
- Export a friend: use the `wxid_*` target shown by `list-targets`.
- Export a group: use the `*@chatroom` target shown by `list-targets`.
- Limit a date range: add `--since YYYY-MM-DD --until YYYY-MM-DD`.
- Skip attachment copying: add `--no-attachments`.
- Keep existing daily files untouched: add `--mode skip`.

## Safety Defaults

- Key capture omits raw PBKDF passwords and creates key logs with `0600` permissions.
- Export paths are forced to stay inside the specified Obsidian vault.
- Existing Markdown files are overwritten only by default deterministic export behavior; use `--mode skip` to preserve them.
- Attachment copying preserves relative paths to avoid filename collisions.

## References

Read `references/schema-and-limits.md` only when debugging WeChat storage layout, SQLCipher decryption details, table discovery, or unsupported message/attachment cases.

## Credits

This rewrite is based on the workflow demonstrated by `Jane-xiaoer/wechat-to-obsidian` and the SQLCipher key extraction approach credited there to `zhuyansen/wx-favorites-report`. Keep those credits when redistributing derived versions.
