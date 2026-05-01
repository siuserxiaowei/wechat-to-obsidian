# Upstream Projects

This skill was improved by reading these public projects and their documentation:

| Project | Role in this skill | License note |
| --- | --- | --- |
| `Jane-xiaoer/wechat-to-obsidian` | Original skill/workflow inspiration for WeChat Mac 4.x key capture, DB decrypt, and Obsidian export. | MIT in the upstream repository. |
| `zhuyansen/wx-favorites-report` | Publicly credited source of the Frida `CCKeyDerivationPBKDF` key-capture approach. | Preserve attribution when discussing the key-capture method. |
| `hicccc77/WeFlow` | Reference for local WeChat export workflow, JSON formats, ChatLab pull shape, and HTTP API endpoints. | CC BY-NC-SA 4.0 / non-commercial. Prefer interoperability through exported JSON and local HTTP API. |
| `ILoveBingLu/CipherTalk` | Reference for local WeChat viewing/export concepts and WeFlow lineage. | CC BY-NC-SA 4.0 / non-commercial. Do not silently mix copied implementation into MIT code. |

## Compatibility Notes

- WeFlow's local API defaults to `http://127.0.0.1:5031`.
- Relevant WeFlow endpoints include `/api/v1/sessions`, `/api/v1/messages`, and `/api/v1/sessions/:id/messages`.
- WeFlow stores app config at `~/Library/Application Support/weflow/WeFlow-config.json` on macOS. This skill reads only API host, port, and token fields from that file.
- WeFlow may return either its raw messages shape or ChatLab-compatible shapes. Keep importers permissive and field-driven.
- If future updates directly port non-trivial WeFlow or CipherTalk implementation code, update repository licensing/attribution before redistribution.
