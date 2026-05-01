---
name: wechat-to-obsidian
description: Export WeChat local data into an Obsidian vault. Prefer jackwener/wx-cli for chat history, with local wechat-cli package fallback, then WeFlow JSON/API, then direct database decryption.
---

# WeChat To Obsidian

Use this skill to move the user's own WeChat chats, File Transfer Assistant, links, files, screenshots, and learning material into an Obsidian vault as Markdown with attachments. The CLI is `scripts/wechat2obsidian.py`.

Only operate on the user's own local WeChat data. Do not upload decrypted databases, key logs, attachments, raw CLI output, or exported vault contents to third-party services.

## Preferred Workflow

Use this order:

1. `jackwener/wx-cli`
2. Local `wechat-cli-pkg.tar.gz` binary via `--binary`
3. WeFlow JSON/API
4. Direct WeChat DB mode

## wx-cli Route

Install and initialize:

```bash
npm install -g @jackwener/wx-cli
codesign --force --deep --sign - /Applications/WeChat.app
killall WeChat && open /Applications/WeChat.app
sudo wx init
```

List sessions:

```bash
python3 scripts/wechat2obsidian.py wx-sessions --limit 100
```

Import File Transfer Assistant:

```bash
python3 scripts/wechat2obsidian.py import-wx-cli \
  --chat filehelper \
  --vault ~/Documents/Obsidian \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --since YYYY-MM-DD \
  --until YYYY-MM-DD \
  --media
```

Import a group or friend:

```bash
python3 scripts/wechat2obsidian.py import-wx-cli \
  --chat "群名称或 wxid/chatroom id" \
  --vault ~/Documents/Obsidian \
  --folder "微信渠道" \
  --subfolder "重要群聊/群名"
```

## Local wechat-cli Package Fallback

If `wx` is unavailable, unpack the user-provided package and pass the binary:

```bash
tar -xzf /path/to/wechat-cli-pkg.tar.gz -C /tmp/wechat-cli-pkg

python3 scripts/wechat2obsidian.py import-wx-cli \
  --binary /tmp/wechat-cli-pkg/wechat-cli-pkg/wechat-cli/node_modules/@canghe_ai/wechat-cli-darwin-arm64/bin/wechat-cli \
  --chat "群名称或文件传输助手" \
  --vault ~/Documents/Obsidian \
  --folder "微信渠道" \
  --subfolder "wechat-cli导入"
```

## WeFlow Compatibility

Import WeFlow JSON:

```bash
python3 scripts/wechat2obsidian.py import-weflow-json \
  --input ~/Downloads/weflow-export.json \
  --vault ~/Documents/Obsidian \
  --folder "微信渠道"
```

Import via WeFlow local API:

```bash
python3 scripts/wechat2obsidian.py weflow-sessions --keyword 文件
python3 scripts/wechat2obsidian.py import-weflow-api \
  --talker filehelper \
  --vault ~/Documents/Obsidian \
  --folder "微信渠道" \
  --subfolder "文件传输助手" \
  --media
```

## Direct DB Fallback

Use direct DB mode only when wx-cli/wechat-cli/WeFlow cannot satisfy the task. It requires signing WeChat, capturing keys, decrypting `message_*.db`, discovering targets, and exporting.

## Common Tasks

- List wx-cli sessions: `wx-sessions`.
- Import from wx-cli: `import-wx-cli`.
- Import from existing wx-cli JSON: `import-wx-cli --input-json history.json`.
- Import existing WeFlow JSON: `import-weflow-json`.
- Import live from WeFlow local API: `import-weflow-api`.
- Keep edited daily files: add `--mode skip`.

## References

Read `references/schema-and-limits.md` only when debugging WeChat storage layout, wx-cli/WeFlow interop, SQLCipher decryption details, table discovery, or unsupported message/attachment cases.

Read `references/upstream-projects.md` when updating credits, license notes, or upstream compatibility with Jane-xiaoer/wechat-to-obsidian, jackwener/wx-cli, WeFlow, CipherTalk, or wx-favorites-report.
