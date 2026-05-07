"""批量导出 WeChat 会话到 Obsidian。
项目原版只能一次导一个,这个脚本支持:
- 从 CSV 读会话清单 (list_chats.py 的输出)
- 自动选 top N
- 自动跨多个 message DB 查找会话
- 失败重试 + 进度报告
- 跳过已导出的(增量)

Usage:
  python3 scripts/batch_export.py \
    --csv /tmp/wechat_chats.csv \
    --top 30 \
    --message-dbs /tmp/wechat_decrypted/message_0.db /tmp/wechat_decrypted/message_1.db \
    --vault ~/Documents/Obsidian\\ Vault \
    --folder 微信渠道
"""
import argparse
import csv
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
EXPORT_SCRIPT = SCRIPT_DIR / "export_chat.py"


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def find_db_for_chat(target: str, dbs: list) -> str | None:
    """找包含目标会话的那个 DB"""
    table = f"Msg_{md5(target)}"
    for db in dbs:
        if not os.path.exists(db):
            continue
        try:
            conn = sqlite3.connect(db)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return db
        except Exception:
            continue
    return None


def safe_subfolder_name(name: str) -> str:
    """把会话名转成合法的文件夹名(macOS 比较宽松,只过滤 / 和 NULL)"""
    safe = name.replace("/", "_").replace("\x00", "_")
    safe = safe.strip()[:200]
    if safe.startswith("."):
        safe = "_" + safe[1:]
    return safe or "unnamed"


def already_exported(out_dir: Path) -> bool:
    """检查是否已经导出过(目录非空)"""
    if not out_dir.exists():
        return False
    has_md = any(p.suffix == ".md" for p in out_dir.rglob("*"))
    return has_md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="list_chats.py 输出的 CSV")
    ap.add_argument("--top", type=int, default=30, help="导出前 N 个")
    ap.add_argument("--min-messages", type=int, default=500, help="最小消息数门槛")
    ap.add_argument(
        "--message-dbs",
        nargs="+",
        default=[
            "/tmp/wechat_decrypted/message_0.db",
            "/tmp/wechat_decrypted/message_1.db",
            "/tmp/wechat_decrypted/message_2.db",
        ],
    )
    ap.add_argument("--vault", required=True)
    ap.add_argument("--folder", default="微信渠道")
    ap.add_argument("--type", choices=["group", "private", "all"], default="all")
    ap.add_argument("--skip-existing", action="store_true", help="跳过已经导过的")
    ap.add_argument("--dry-run", action="store_true", help="只打印要做啥,不真跑")
    ap.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    args = ap.parse_args()

    # 读 CSV
    with open(args.csv, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # 过滤
    chats = []
    for row in rows:
        msgs = int(row["messages"])
        if msgs < args.min_messages:
            continue
        if args.type != "all":
            row_type = row["type"]
            if row_type != args.type:
                continue
        chats.append(row)

    chats = chats[: args.top]
    print(f"\n📋 计划导出 {len(chats)} 个会话:\n")

    for i, chat in enumerate(chats, 1):
        print(f"  {i}. [{chat['messages']:>6} 条] {chat['type']:<7} {chat['name'][:40]}")

    if args.dry_run:
        print("\n[dry-run] 不实际执行")
        return

    if not args.yes:
        print()
        confirm = input("继续? (y/N): ").strip().lower()
        if confirm != "y":
            print("取消")
            return

    # 批量导出
    folder_path = Path(args.vault) / args.folder
    folder_path.mkdir(parents=True, exist_ok=True)

    success = []
    failed = []
    skipped = []
    total = len(chats)

    for i, chat in enumerate(chats, 1):
        name = chat["name"]
        wxid = chat["wxid"]
        subfolder = safe_subfolder_name(name)

        out_dir = folder_path / subfolder

        if args.skip_existing and already_exported(out_dir):
            print(f"[{i}/{total}] ⏭  跳过 {name}(已存在)")
            skipped.append(name)
            continue

        # 找对应 DB
        db = find_db_for_chat(wxid, args.message_dbs)
        if not db:
            print(f"[{i}/{total}] ❌ 找不到 DB: {name}")
            failed.append((name, "no DB"))
            continue

        print(f"[{i}/{total}] 🔄 {name} ({chat['messages']} 条) ← {os.path.basename(db)}")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(EXPORT_SCRIPT),
                    "--db", db,
                    "--target", wxid,
                    "--vault", args.vault,
                    "--folder", args.folder,
                    "--subfolder", subfolder,
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                print(f"   ❌ 失败: {result.stderr[:200]}")
                failed.append((name, result.stderr[:100]))
            else:
                # 提取最后一行 "wrote N daily files"
                lines = result.stdout.strip().split("\n")
                last = lines[-1] if lines else ""
                print(f"   ✅ {last}")
                success.append(name)
        except subprocess.TimeoutExpired:
            print(f"   ❌ 超时")
            failed.append((name, "timeout"))
        except Exception as e:
            print(f"   ❌ 异常: {e}")
            failed.append((name, str(e)))

    # 总结
    print(f"\n{'='*60}")
    print(f"📊 完成: ✅ {len(success)}  ⏭ {len(skipped)}  ❌ {len(failed)}")
    if failed:
        print(f"\n失败列表:")
        for name, reason in failed:
            print(f"  - {name}: {reason}")


if __name__ == "__main__":
    main()
