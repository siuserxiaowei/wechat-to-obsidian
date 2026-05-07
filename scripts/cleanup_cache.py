"""清理 WeChat 老缓存(按月份),安全释放磁盘空间。

WeChat 的 cache/ 目录按月组织(2024-08, 2024-09, ...),里面是图片/视频缩略图。
清掉老月份的缓存对 WeChat 没影响 — 用户下次需要看老消息时,WeChat 会从原始数据重新生成缩略图。

策略:
- 默认保留最近 N 个月(默认 3 个月)
- 超过 N 个月的缓存目录直接删
- 列出大小 + 让用户看清楚要删什么

⚠️ 这是真删除(不是硬链接合并),释放真实空间。

Usage:
  # 预演
  python3 scripts/cleanup_cache.py \
    --root ~/Library/Containers/.../xwechat_files/wxid_xxx/cache \
    --keep-months 3

  # 执行
  python3 scripts/cleanup_cache.py \
    --root ~/Library/Containers/.../xwechat_files/wxid_xxx/cache \
    --keep-months 3 \
    --apply
"""
import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path


def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def folder_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="cache/ 路径")
    ap.add_argument("--keep-months", type=int, default=3, help="保留最近 N 个月")
    ap.add_argument("--apply", action="store_true", help="实际删除")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"[!] 目录不存在: {root}")
        return

    # 按月份目录排序(YYYY-MM 格式)
    month_dirs = sorted(
        [d for d in root.iterdir() if d.is_dir() and len(d.name) == 7 and d.name[4] == "-"],
        key=lambda d: d.name,
        reverse=True,
    )

    if not month_dirs:
        print(f"[!] 未找到 YYYY-MM 格式的子目录")
        return

    print(f"📅 发现 {len(month_dirs)} 个月份目录")
    print(f"💾 总大小: {human_size(folder_size(root))}")
    print()

    keep = month_dirs[: args.keep_months]
    delete = month_dirs[args.keep_months:]

    print(f"✅ 保留最近 {args.keep_months} 个月:")
    for d in keep:
        size = folder_size(d)
        print(f"  {d.name}: {human_size(size)}")
    print()

    print(f"🗑 计划删除 {len(delete)} 个老月份:")
    total_to_delete = 0
    for d in delete:
        size = folder_size(d)
        total_to_delete += size
        print(f"  {d.name}: {human_size(size)}")

    print(f"\n💾 可释放: {human_size(total_to_delete)}")

    if not args.apply:
        print(f"\n💡 用 --apply 实际删除")
        return

    print(f"\n🔧 开始删除...")
    deleted = 0
    for d in delete:
        try:
            shutil.rmtree(d)
            deleted += 1
        except Exception as e:
            print(f"[!] 删除失败 {d}: {e}")

    print(f"\n✅ 删除了 {deleted} 个目录,释放约 {human_size(total_to_delete)}")


if __name__ == "__main__":
    main()
