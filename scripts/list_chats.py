"""列出 WeChat 解密后的所有会话,按消息数排序,显示真实名字。

Usage:
  python3 scripts/list_chats.py \
    --message-db /tmp/wechat_decrypted/message_0.db \
    --contact-db /tmp/wechat_decrypted/contact.db \
    [--message-dbs message_0.db message_1.db message_2.db]  # 多个消息库
    [--top 50]   # 只看前 N 个
    [--type group|private|all]  # 过滤群/私聊/全部
"""
import argparse
import hashlib
import os
import sqlite3
import sys


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def load_contacts(contact_db_path: str) -> dict:
    """从 contact.db 加载 wxid → 名字 映射"""
    if not os.path.exists(contact_db_path):
        return {}
    conn = sqlite3.connect(contact_db_path)
    cursor = conn.cursor()

    name_map = {}

    # 私聊/群: contact 表
    cursor.execute("""
        SELECT username, COALESCE(NULLIF(remark, ''), nick_name, alias, username)
        FROM contact
    """)
    for username, name in cursor.fetchall():
        if username and name:
            name_map[username] = name

    # 群名: chat_room 表里没有 nickname, 名字其实在 contact 表里 (chatroom 也是 contact)

    conn.close()
    return name_map


def list_chats_in_db(message_db_path: str) -> dict:
    """扫描 message DB, 返回 {Msg_<hash>: row_count}"""
    conn = sqlite3.connect(message_db_path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
    )
    tables = [t[0] for t in cursor.fetchall()]

    counts = {}
    for table in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cursor.fetchone()[0]
        except Exception:
            counts[table] = -1

    # 也加载 Name2Id 映射 (反向: hash → original name)
    cursor.execute("SELECT user_name, is_session FROM Name2Id")
    name2id = {}
    for user_name, is_session in cursor.fetchall():
        h = md5(user_name)
        name2id[f"Msg_{h}"] = (user_name, is_session)

    conn.close()
    return counts, name2id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--message-dbs",
        nargs="+",
        default=["/tmp/wechat_decrypted/message_0.db"],
        help="一个或多个解密后的 message DB",
    )
    ap.add_argument(
        "--contact-db",
        default="/tmp/wechat_decrypted/contact.db",
        help="解密后的 contact.db",
    )
    ap.add_argument("--top", type=int, default=50, help="只看消息数前 N 多的会话")
    ap.add_argument(
        "--type",
        choices=["group", "private", "all"],
        default="all",
        help="过滤群/私聊/全部",
    )
    ap.add_argument("--min-messages", type=int, default=10, help="最小消息数门槛")
    ap.add_argument("--csv", help="输出 CSV 路径")
    args = ap.parse_args()

    # 加载联系人
    contacts = load_contacts(args.contact_db)
    print(f"[*] 加载 {len(contacts)} 个联系人/群名", file=sys.stderr)

    # 扫描所有 message DB
    all_chats = {}  # target → (display_name, total_count, dbs)

    for db_path in args.message_dbs:
        if not os.path.exists(db_path):
            print(f"[!] DB 不存在: {db_path}", file=sys.stderr)
            continue

        print(f"[*] 扫描 {db_path}...", file=sys.stderr)
        counts, name2id = list_chats_in_db(db_path)

        for table, count in counts.items():
            if table not in name2id:
                continue
            user_name, _ = name2id[table]

            if user_name not in all_chats:
                display = contacts.get(user_name, user_name)
                all_chats[user_name] = {
                    "display": display,
                    "wxid": user_name,
                    "total": 0,
                    "dbs": [],
                }

            all_chats[user_name]["total"] += count
            all_chats[user_name]["dbs"].append(os.path.basename(db_path))

    # 过滤
    chats = []
    for wxid, info in all_chats.items():
        if info["total"] < args.min_messages:
            continue
        is_group = "@chatroom" in wxid
        if args.type == "group" and not is_group:
            continue
        if args.type == "private" and is_group:
            continue
        chats.append(info)

    # 排序
    chats.sort(key=lambda x: -x["total"])

    # 输出
    print(f"\n📊 共 {len(chats)} 个会话(消息≥{args.min_messages})\n")
    print(f"{'序号':<5} {'消息数':<8} {'类型':<6} {'名称':<40} {'wxid'}")
    print("-" * 100)

    for i, chat in enumerate(chats[: args.top], 1):
        chat_type = "群" if "@chatroom" in chat["wxid"] else "私聊"
        if chat["wxid"] == "filehelper":
            chat_type = "文件助手"
        name = chat["display"][:38] if chat["display"] else "(无名)"
        print(
            f"{i:<5} {chat['total']:<8} {chat_type:<6} {name:<40} {chat['wxid']}"
        )

    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["rank", "messages", "type", "name", "wxid", "dbs"])
            for i, chat in enumerate(chats, 1):
                chat_type = "group" if "@chatroom" in chat["wxid"] else "private"
                writer.writerow([
                    i,
                    chat["total"],
                    chat_type,
                    chat["display"],
                    chat["wxid"],
                    ";".join(set(chat["dbs"])),
                ])
        print(f"\n💾 完整列表已写入 {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
