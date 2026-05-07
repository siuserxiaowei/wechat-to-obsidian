#!/bin/bash
# 一键抓 WeChat SQLCipher 密钥
# 运行后:
#   1. 自动 kill 现有 WeChat
#   2. 启动 frida hook（5分钟超时）
#   3. 启动重签的 WeChat 副本
#   4. 监控 keys 日志
#
# 你要做的:
#   登录 → 依次点开你想导出的会话（"文件传输助手"、群聊、私聊）
#   每点一次,frida 就会抓到对应 DB 的 key

set -e

PROJECT_DIR="${PROJECT_DIR:-$HOME/Workspace/wechat-to-obsidian}"
SIGNED_WECHAT="${SIGNED_WECHAT:-$HOME/Desktop/WeChat.app}"
KEYS_LOG="/tmp/wechat_keys.log"

cd "$PROJECT_DIR"
source .venv/bin/activate

echo "🔒 1️⃣ 关闭现有 WeChat..."
killall WeChat 2>/dev/null || echo "（没有运行中的 WeChat）"
sleep 2

echo "🪝 2️⃣ 启动 frida hook（后台跑,5分钟超时）..."
> "$KEYS_LOG"  # 清空之前的日志
python3 scripts/extract_key.py --out "$KEYS_LOG" --wait 300 &
FRIDA_PID=$!
echo "   frida PID: $FRIDA_PID"
sleep 3

echo "🚀 3️⃣ 启动重签的 WeChat 副本..."
"$SIGNED_WECHAT/Contents/MacOS/WeChat" &
WX_PID=$!
echo "   WeChat PID: $WX_PID"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  👉 现在轮到你了:"
echo ""
echo "  1. 在弹出的 WeChat 里登录"
echo "  2. 依次点开你想导出的会话:"
echo "     - 文件传输助手"
echo "     - 收藏"
echo "     - MH-2026赛季群"
echo "     - 其他重要群/私聊"
echo "  3. 每点开一个,frida 就抓到一把钥匙"
echo "  4. 全部点完后,在另一个终端跑:"
echo "     cat $KEYS_LOG | grep -c 'salt='"
echo "     看抓到几把"
echo ""
echo "  ⏰ frida 自动 5 分钟超时,够你慢慢点"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 实时监控 keys log
echo ""
echo "📊 实时监控密钥捕获:"
tail -f "$KEYS_LOG"
