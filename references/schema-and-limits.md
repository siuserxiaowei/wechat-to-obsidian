# Schema And Limits

Use this reference when the normal workflow fails or when adapting the exporter to another WeChat build.

## Local Paths

Default WeChat 4.x data root:

```text
~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files
```

Each logged-in user normally appears as a `wxid_*` directory. The CLI chooses the largest user directory when more than one is present; pass `--base` to `locate-user` or `--wechat-root` to `export-chat` when the auto choice is wrong.

Common databases:

```text
<user>/db_storage/message/message_0.db
<user>/db_storage/contact/contact.db
<user>/db_storage/favorite/favorite.db
<user>/db_storage/session/session.db
```

## SQLCipher Notes

WeChat Mac 4.x uses SQLCipher v4-style pages:

- Page size: 4096 bytes.
- First 16 bytes of the encrypted DB are the salt.
- Each encrypted page reserves 80 trailing bytes.
- The IV is the first 16 bytes of the reserved trailer.
- Message database keys are derived through `CCKeyDerivationPBKDF` with 256000 rounds.

The bundled decryptor reconstructs a normal SQLite database and runs a lightweight SQLite verification unless `--no-verify` is passed.

## Message Tables

Conversation rows live in tables named:

```text
Msg_<md5(target_id)>
```

Examples:

```text
filehelper -> Msg_9e20f478899dc29eb19741386f9343c8
12345@chatroom -> Msg_<md5("12345@chatroom")>
```

`Name2Id` usually maps a WeChat `user_name` to the corresponding session id. Use `list-targets` first instead of guessing target ids.

## wx-cli Session Accuracy

`wx history <CHAT>` supports fuzzy matching, so display names are not safe identifiers when multiple groups share a name. Prefer this flow:

```bash
python3 scripts/wechat2obsidian.py wx-sessions --limit 500 --json
python3 scripts/wechat2obsidian.py import-wx-cli --chat-id "12345@chatroom" ...
```

`import-wx-cli` resolves `--chat-name` through `wx-sessions` and refuses ambiguous matches. It also fetches `wx history` page by page with `--offset`, deduplicates messages by `local_id` or a timestamp/sender/content fingerprint, then writes audit fields to `_wx_cli_import_manifest.json`: `resolved_session`, `pages_fetched`, `raw_message_count`, `deduped_count`, `filtered_count`, `dropped_count`, `first_message_at`, `last_message_at`, `warnings`, and `raw_debug`.

If multiple `wxid_*` directories exist under `xwechat_files`, `locate-user` and `doctor` list the candidates with key database status instead of guessing the largest directory. Pass the intended path explicitly with `--base` or `--wechat-root` when using direct DB export.

## Message Types

Frequently seen `local_type` values:

| local_type | Meaning |
| --- | --- |
| 1 | text |
| 3 | image |
| 34 | voice |
| 43 | video |
| 47 | emoji |
| 48 | location |
| 49 | share card, article, mini-program, file, quote |
| 10000 | system message |
| 65537 | system notice |

WeChat content can be raw UTF-8 text, XML-like fragments, protobuf-like bytes, or zstd-compressed bytes. The exporter decodes zstd when the frame magic is present and then renders the best Markdown representation it can.

## Known Limits

- macOS only. Windows, Android, and iOS use different storage and key paths.
- The Frida hook must be active while the relevant WeChat surface opens; WeChat loads many databases lazily.
- Cloud-only media, video-account material, some mini-program data, and CDN-only originals may not exist in local storage.
- The exporter copies local attachment files but cannot always map every media message to a specific original file.
- Re-sign the copied app after major WeChat upgrades or when macOS invalidates the ad-hoc signature.

## WeFlow Interop

WeFlow can be used as an upstream local data source when the user has it installed:

- Import an exported WeFlow JSON file with `import-weflow-json`.
- Pull data from the local WeFlow HTTP API with `import-weflow-api`.
- List API sessions with `weflow-sessions`.
- The API default base URL is `http://127.0.0.1:5031`.
- The CLI auto-reads `~/Library/Application Support/weflow/WeFlow-config.json` for `httpApiHost`, `httpApiPort`, and `httpApiToken` when present.
- If WeFlow has an access token configured, pass `--token`, set `WEFLOW_TOKEN`, or let the CLI read it from config. If the token is empty, calls are sent without auth.

The importer handles these common shapes:

- WeFlow HTTP `/api/v1/messages` JSON: `success`, `talker`, `messages[]`.
- WeFlow ChatLab Pull API: `/api/v1/sessions/:id/messages`, `meta`, `members[]`, `messages[]`, `sync`.
- WeFlow detailed JSON export: `session`, `weflow`, `avatars`, `messages[]`.
- ChatLab-style JSON: `chatlab`, `meta`, `members[]`, `messages[]`.

For redistributable code, prefer API/file interoperability over copying WeFlow or CipherTalk implementation. Both upstream projects use non-commercial Creative Commons-style licensing.

## Troubleshooting

- `No matching key`: rerun `capture-keys`, then open the target conversation or favorites page inside WeChat while the hook is active.
- `Table Msg_<hash> not found`: run `list-targets` on the decrypted DB and use an exact target id.
- `frida attach failed`: confirm the signed copy is running, close the App Store copy, and grant Terminal/Codex app permissions under macOS Privacy & Security if prompted.
- `SQLite verification failed`: the wrong key was used, the DB changed during copy, or the WeChat build changed the page layout. Copy the DB after quitting WeChat and retry.
