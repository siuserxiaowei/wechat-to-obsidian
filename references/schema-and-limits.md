# Schema And Limits

Use this reference when the normal workflow fails or when adapting the exporter to another WeChat build.

## Local Paths

Default WeChat 4.x data root:

```text
~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files
```

Each logged-in user normally appears as a `wxid_*` directory. The CLI chooses the largest user directory when more than one is present; pass `--user-dir` or `--wechat-root` when the auto choice is wrong.

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
filehelper -> Msg_9e20f4783836bdbf1112e2707878b8d8
12345@chatroom -> Msg_<md5("12345@chatroom")>
```

`Name2Id` usually maps a WeChat `user_name` to the corresponding session id. Use `list-targets` first instead of guessing target ids.

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

## Troubleshooting

- `No matching key`: rerun `capture-keys`, then open the target conversation or favorites page inside WeChat while the hook is active.
- `Table Msg_<hash> not found`: run `list-targets` on the decrypted DB and use an exact target id.
- `frida attach failed`: confirm the signed copy is running, close the App Store copy, and grant Terminal/Codex app permissions under macOS Privacy & Security if prompted.
- `SQLite verification failed`: the wrong key was used, the DB changed during copy, or the WeChat build changed the page layout. Copy the DB after quitting WeChat and retry.
