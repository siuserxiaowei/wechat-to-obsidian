# WeChat → Obsidian 完整工具链使用说明

> 这套工具的扩展版本：原项目能 **导出单个会话**，本项目额外加了：
> - 列出所有会话（`list_chats.py`）
> - 批量导出（`batch_export.py`）
> - 附件去重（`dedup_attachments.py`）
> - 按类型分类（`categorize_attachments.py`）

---

## 一次性 Setup（半小时）

跟原 README 一样：

```bash
# 1. 装依赖
cd ~/Workspace/wechat-to-obsidian
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 复制 + 重签 WeChat
cp -R /Applications/WeChat.app ~/Desktop/WeChat.app
xattr -rc ~/Desktop/WeChat.app
codesign --force --deep --sign - ~/Desktop/WeChat.app

# 3. 抓密钥
./run_capture_key.sh
# 在弹出的 WeChat 里登录,点开你想要的会话,frida 自动抓
```

---

## 日常用法（5 分钟）

每次想刷新数据，重复以下：

### Step 1：解密 DB

```bash
USER_DIR="$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_276exkqyuyd422_20a2"
mkdir -p /tmp/wechat_decrypted

for db in message_0 message_1 message_2 biz_message_0; do
  python3 scripts/decrypt_db.py \
    --db "$USER_DIR/db_storage/message/${db}.db" \
    --keys-log /tmp/wechat_keys.log \
    --out "/tmp/wechat_decrypted/${db}.db"
done

python3 scripts/decrypt_db.py \
  --db "$USER_DIR/db_storage/contact/contact.db" \
  --keys-log /tmp/wechat_keys.log \
  --out "/tmp/wechat_decrypted/contact.db"
```

### Step 2：列出所有会话

```bash
python3 scripts/list_chats.py \
  --message-dbs /tmp/wechat_decrypted/message_0.db \
                /tmp/wechat_decrypted/message_1.db \
                /tmp/wechat_decrypted/message_2.db \
  --contact-db /tmp/wechat_decrypted/contact.db \
  --top 50 \
  --csv /tmp/wechat_chats.csv
```

输出：按消息数排名的会话列表，附 wxid。

### Step 3：批量导出

```bash
python3 scripts/batch_export.py \
  --csv /tmp/wechat_chats.csv \
  --top 30 \
  --min-messages 1000 \
  --vault ~/Documents/Obsidian\ Vault \
  --folder 微信渠道 \
  --skip-existing \
  --yes
```

参数：
- `--top 30` — 只导前 30 个最活跃会话
- `--min-messages 1000` — 至少 1000 条消息才导
- `--skip-existing` — 已经导过的跳过（增量更新）
- `--dry-run` — 只看清单不真跑

### Step 4：附件去重（新增）

```bash
# 扫描 + 出报告
python3 scripts/dedup_attachments.py \
  --root ~/Documents/Obsidian\ Vault/微信渠道 \
  --report /tmp/dedup_report.md

# 看完报告觉得 OK,再实际去重(用硬链接保留所有目录的引用)
python3 scripts/dedup_attachments.py \
  --root ~/Documents/Obsidian\ Vault/微信渠道 \
  --apply --link-mode hardlink
```

### Step 5：按类型分类视图（新增）

```bash
python3 scripts/categorize_attachments.py \
  --root ~/Documents/Obsidian\ Vault/微信渠道
```

会在 `微信渠道/_by_type/` 下生成软链接：
```
_by_type/
├── documents/    (PDF, Word, Excel, ...)
├── images/       (jpg, png, webp, ...)
├── videos/       (mp4, mov, ...)
├── audios/       (mp3, m4a, ...)
├── archives/     (zip, rar, ...)
├── code/         (py, js, ...)
└── others/
```

每类下面再按会话名分组。原文件不动，只是建索引视图。

---

## 一次性升级（每次微信大版本更新后）

```bash
# 删旧的 + 重新签名
rm -rf ~/Desktop/WeChat.app
cp -R /Applications/WeChat.app ~/Desktop/WeChat.app
xattr -rc ~/Desktop/WeChat.app
codesign --force --deep --sign - ~/Desktop/WeChat.app
```

然后再跑 `run_capture_key.sh` 抓新密钥即可。

---

## 我加的脚本 vs 原项目

| 脚本 | 来源 | 干什么 |
|------|------|--------|
| `extract_key.py` | 原项目 | frida hook 抓 SQLCipher 密钥 |
| `decrypt_db.py` | 原项目 | AES 解密 DB |
| `export_chat.py` | 原项目 | 导出单个会话 |
| `list_chats.py` | **新加** | 列出全部会话 + 真实名字 |
| `batch_export.py` | **新加** | 批量导出 + 增量 + 容错 |
| `dedup_attachments.py` | **新加** | SHA-256 去重 + 报告 |
| `categorize_attachments.py` | **新加** | 按文件类型分类视图 |
| `run_capture_key.sh` | **新加** | 一键启动密钥捕获 |
