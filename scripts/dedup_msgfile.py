"""清理微信原始 msg/file/ 目录里的重复文件,真实释放磁盘空间。

策略:
- 按 SHA-256 分组找重复
- 保留每组中"最早"的那个(creation time 最早)
- 其他副本: 删除并替换为硬链接指向保留版本
- 结果: 所有原文件名仍可见, 但共享同一个 inode → 磁盘空间释放

为什么用硬链接而不是直接删:
- 微信"我的文件"视图依赖这些文件存在 → 删除会让文件消失
- 硬链接: 文件还在原位置可见,只是不重复占空间
- 完全可逆: 想恢复? 把硬链接 cp 一份就回来了

⚠️ 安全保护:
- 默认 dry-run, 不真做
- 显式 --apply 才执行
- 自动备份原始 inode 信息到 backup 文件
- 只处理满足 min-size 的文件(默认 10KB)

Usage:
  # 预演
  python3 scripts/dedup_msgfile.py \
    --root ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_xxx/msg/file

  # 实际执行
  python3 scripts/dedup_msgfile.py \
    --root ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_xxx/msg/file \
    --apply
"""
import argparse
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def hash_file(path: Path, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="msg/file/ 路径")
    ap.add_argument("--min-size", type=int, default=10240, help="忽略小于 N 字节的文件")
    ap.add_argument("--apply", action="store_true", help="实际执行(默认 dry-run)")
    ap.add_argument(
        "--backup",
        default=os.path.expanduser("~/.wechat-to-obsidian/dedup_backup.json"),
        help="备份原始 inode 信息(用于 undo)",
    )
    ap.add_argument(
        "--report",
        default=os.path.expanduser("~/.wechat-to-obsidian/dedup_msgfile_report.md"),
    )
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"[!] 目录不存在: {root}")
        return

    print(f"🔍 扫描 {root}")

    # 按大小预过滤(同 hash 必同大小)
    by_size = defaultdict(list)
    total_files = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size < args.min_size:
            continue
        by_size[stat.st_size].append((path, stat))
        total_files += 1

    print(f"[*] 共 {total_files} 个文件,跨 {len(by_size)} 个不同大小")

    # 同大小算 hash
    by_hash = defaultdict(list)
    hashed = 0
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        for path, stat in items:
            try:
                h = hash_file(path)
                # 同一 inode 不重复算
                by_hash[h].append({
                    "path": path,
                    "inode": stat.st_ino,
                    "size": stat.st_size,
                    "ctime": stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_ctime,
                })
                hashed += 1
            except Exception as e:
                print(f"[!] hash 失败 {path}: {e}")

    print(f"[*] 已 hash {hashed} 个候选")

    # 找重复(同 hash, 不同 inode)
    real_dupes = {}
    total_waste = 0
    for h, items in by_hash.items():
        # 按 inode 分组
        by_inode = defaultdict(list)
        for item in items:
            by_inode[item["inode"]].append(item)
        if len(by_inode) < 2:
            continue
        # 多个 inode 才是真重复
        real_dupes[h] = items
        # 浪费 = 总大小 - 单份大小 (多余的 inode 数 * size)
        size = items[0]["size"]
        waste = size * (len(by_inode) - 1)
        total_waste += waste

    print(f"\n📊 找到 {len(real_dupes)} 组真实重复(不同 inode)")
    print(f"💾 可释放空间: {human_size(total_waste)}\n")

    # 生成报告
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(f"# msg/file/ 去重报告\n\n")
        f.write(f"- 扫描根: `{root}`\n")
        f.write(f"- 候选文件: {total_files}\n")
        f.write(f"- 重复组数: {len(real_dupes)}\n")
        f.write(f"- 可释放: **{human_size(total_waste)}**\n")
        f.write(f"- 模式: {'apply' if args.apply else 'dry-run'}\n\n")

        sorted_dupes = sorted(
            real_dupes.items(),
            key=lambda kv: -kv[1][0]["size"] * (len(set(i["inode"] for i in kv[1])) - 1),
        )
        f.write(f"## TOP 50 重复\n\n")
        for h, items in sorted_dupes[:50]:
            inodes = set(i["inode"] for i in items)
            size = items[0]["size"]
            waste = size * (len(inodes) - 1)
            f.write(f"### `{items[0]['path'].name}` ({human_size(size)} × {len(inodes)} inodes, 浪费 {human_size(waste)})\n\n")
            for item in items:
                rel = item["path"].relative_to(root)
                f.write(f"- inode={item['inode']} `{rel}`\n")
            f.write("\n")

    print(f"📝 报告: {args.report}")

    if not args.apply:
        print(f"\n💡 用 --apply 实际执行")
        return

    # 实际执行
    print(f"\n🔧 开始去重(硬链接合并)...")
    backup_data = {}
    processed = 0
    failed = []

    for h, items in real_dupes.items():
        # 选 keep: 最早创建的(ctime 最小)
        items_sorted = sorted(items, key=lambda x: x["ctime"])
        keep = items_sorted[0]
        keep_inode = keep["inode"]

        for item in items_sorted[1:]:
            if item["inode"] == keep_inode:
                # 已经是硬链接,跳过
                continue
            try:
                # 备份这个文件的 inode 信息
                backup_data[str(item["path"])] = {
                    "old_inode": item["inode"],
                    "size": item["size"],
                    "hash": h,
                    "merged_to": str(keep["path"]),
                    "merged_to_inode": keep_inode,
                }
                # 删除 + 硬链接
                item["path"].unlink()
                os.link(keep["path"], item["path"])
                processed += 1
            except Exception as e:
                failed.append((str(item["path"]), str(e)))
                print(f"[!] 失败 {item['path']}: {e}")

    # 写备份
    Path(args.backup).parent.mkdir(parents=True, exist_ok=True)
    backup_full = {
        "timestamp": datetime.now().isoformat(),
        "root": str(root),
        "operations": backup_data,
    }
    # 追加模式: 如果已有备份,合并
    existing = []
    if Path(args.backup).exists():
        try:
            existing_raw = json.loads(Path(args.backup).read_text())
            if isinstance(existing_raw, list):
                existing = existing_raw
            else:
                existing = [existing_raw]
        except Exception:
            pass
    existing.append(backup_full)
    Path(args.backup).write_text(json.dumps(existing, ensure_ascii=False, indent=2))

    print(f"\n✅ 完成:")
    print(f"  处理: {processed} 个文件合并为硬链接")
    print(f"  失败: {len(failed)}")
    print(f"  备份: {args.backup}")
    print(f"\n💾 实际释放空间: {human_size(total_waste)}")
    print(f"\n🔄 想撤销? 见: scripts/dedup_msgfile_undo.py")


if __name__ == "__main__":
    main()
