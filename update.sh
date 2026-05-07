#!/bin/bash
# 自动增量更新 WeChat → Obsidian
# 跳过密钥抓取(假设密钥已经在 ~/.wechat-to-obsidian/keys.log)
# 跑: 解密 → 列表 → 增量批量导出 → 增量真实文件导入
#
# 环境变量(可选):
#   WX_USER_WXID  - 你的 wxid 子目录名(默认自动检测最大的)
#   VAULT         - Obsidian vault 路径(默认 ~/Documents/Obsidian Vault)
#   PROJECT_DIR   - 工程根(默认 ~/Workspace/wechat-to-obsidian)

set -e

PROJECT_DIR="${PROJECT_DIR:-$HOME/Workspace/wechat-to-obsidian}"
KEYS_LOG="$HOME/.wechat-to-obsidian/keys.log"
DECRYPTED_DIR="/tmp/wechat_decrypted"
WX_FILES_ROOT="$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"

# 自动找最大的 wxid 子目录(数据最多的那个)
if [ -z "${WX_USER_WXID:-}" ]; then
  WX_USER_WXID=$(du -s "$WX_FILES_ROOT"/wxid_*/ 2>/dev/null | sort -rn | head -1 | awk '{print $NF}' | xargs basename 2>/dev/null)
fi
WX_USER="$WX_FILES_ROOT/$WX_USER_WXID"

if [ ! -d "$WX_USER" ]; then
  echo "❌ 找不到微信用户数据目录: $WX_USER"
  echo "   设置环境变量 WX_USER_WXID=wxid_xxx_xxxx 指定"
  exit 1
fi

VAULT="${VAULT:-$HOME/Documents/Obsidian Vault}"
LOG_DIR="$HOME/.wechat-to-obsidian/logs"

mkdir -p "$LOG_DIR" "$DECRYPTED_DIR"
LOG_FILE="$LOG_DIR/update-$(date +%Y%m%d-%H%M%S).log"

echo "🚀 WeChat → Obsidian 增量更新" | tee -a "$LOG_FILE"
echo "时间: $(date)" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

cd "$PROJECT_DIR"
source .venv/bin/activate

# 检查密钥文件
if [ ! -f "$KEYS_LOG" ]; then
  echo "❌ 密钥文件不存在: $KEYS_LOG" | tee -a "$LOG_FILE"
  echo "   请先跑 ./run_capture_key.sh 抓密钥" | tee -a "$LOG_FILE"
  exit 1
fi

# Step 1: 解密所有 message DB
echo "🔓 Step 1: 解密 DB..." | tee -a "$LOG_FILE"
for db in message_0 message_1 message_2 biz_message_0 contact favorite; do
  src="$WX_USER/db_storage/message/${db}.db"
  [ ! -f "$src" ] && src="$WX_USER/db_storage/contact/${db}.db"
  [ ! -f "$src" ] && src="$WX_USER/db_storage/favorite/${db}.db"
  [ ! -f "$src" ] && continue

  python3 scripts/decrypt_db.py \
    --db "$src" \
    --keys-log "$KEYS_LOG" \
    --out "$DECRYPTED_DIR/${db}.db" 2>&1 | tail -2 | tee -a "$LOG_FILE" || true
done

# Step 2: 列出会话
echo "" | tee -a "$LOG_FILE"
echo "📋 Step 2: 列出会话..." | tee -a "$LOG_FILE"
python3 scripts/list_chats.py \
  --message-dbs "$DECRYPTED_DIR/message_0.db" \
                "$DECRYPTED_DIR/message_1.db" \
                "$DECRYPTED_DIR/message_2.db" \
  --contact-db "$DECRYPTED_DIR/contact.db" \
  --top 50 \
  --csv /tmp/wechat_chats.csv 2>&1 | tail -3 | tee -a "$LOG_FILE" || true

# Step 3: 增量批量导出(跳过已存在的)
echo "" | tee -a "$LOG_FILE"
echo "💬 Step 3: 增量导出会话..." | tee -a "$LOG_FILE"
python3 scripts/batch_export.py \
  --csv /tmp/wechat_chats.csv \
  --top 30 \
  --min-messages 1000 \
  --vault "$VAULT" \
  --folder "微信渠道/付费群" \
  --skip-existing \
  --yes 2>&1 | tail -10 | tee -a "$LOG_FILE" || true

# Step 4: 增量真实文件导入(已有硬链接的会跳过)
echo "" | tee -a "$LOG_FILE"
echo "📄 Step 4: 增量导入真实文件..." | tee -a "$LOG_FILE"
python3 scripts/import_real_files.py \
  --wx-user-dir "$WX_USER" \
  --vault "$VAULT" \
  --folder "微信渠道/_文件库" \
  --link-mode hardlink 2>&1 | tail -10 | tee -a "$LOG_FILE" || true

# Step 5: 总结
echo "" | tee -a "$LOG_FILE"
echo "✅ 更新完成 $(date)" | tee -a "$LOG_FILE"
echo "📁 日志: $LOG_FILE" | tee -a "$LOG_FILE"

# 通知(可选: macOS 通知中心)
if command -v osascript &> /dev/null; then
  osascript -e 'display notification "WeChat → Obsidian 增量更新完成" with title "wechat-to-obsidian"' 2>/dev/null || true
fi
