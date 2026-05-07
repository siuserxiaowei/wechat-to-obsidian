"""扫描微信导出的附件文件夹,找出重复文件 + 给出清理建议。

逻辑:
- 按 SHA-256 hash 完全匹配(精确去重)
- 同一个文件可能在不同会话被多次转发 → 都识别出来
- 只生成报告,不自动删除(默认安全)
- 可选 --apply 模式: 保留最早的副本,其他改成软链接

Usage:
  # 扫描 + 生成报告
  python3 scripts/dedup_attachments.py \
    --root "/path/to/Obsidian Vault/微信渠道" \
    --report /tmp/wechat_dedup_report.md

  # 实际去重(保留每组第一个,其他变软链接)
  python3 scripts/dedup_attachments.py \
    --root "/path/to/Obsidian Vault/微信渠道" \
    --apply --link-mode hardlink
"""
import argparse
import hashlib
import os
from collections import defaultdict
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
    ap.add_argument("--root", required=True, help="扫描根目录")
    ap.add_argument("--report", default="/tmp/wechat_dedup_report.md")
    ap.add_argument(
        "--ext",
        nargs="+",
        default=None,
        help="只扫指定扩展名(.pdf .mp4 .jpg ...) 不指定=全部",
    )
    ap.add_argument("--min-size", type=int, default=10240, help="忽略小于 N 字节的文件")
    ap.add_argument("--apply", action="store_true", help="实际去重(默认只生成报告)")
    ap.add_argument(
        "--link-mode",
        choices=["hardlink", "symlink", "delete"],
        default="hardlink",
        help="apply 时的处理方式: 硬链接/软链接/直接删",
    )
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"[!] 目录不存在: {root}")
        return

    print(f"[*] 扫描 {root}...")

    # 先按大小分组(快速排除单一大小的)
    by_size = defaultdict(list)
    total_files = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # 跳过 .md 文件(这些是文本,不算附件)
        if path.suffix.lower() == ".md":
            continue
        # 跳过软链接
        if path.is_symlink():
            continue
        if args.ext:
            if path.suffix.lower() not in [e.lower() for e in args.ext]:
                continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < args.min_size:
            continue
        by_size[size].append(path)
        total_files += 1

    print(f"[*] 共 {total_files} 个候选文件,跨 {len(by_size)} 个不同大小")

    # 同大小的算 hash 找重复
    duplicates = defaultdict(list)
    hashed_files = 0
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for path in paths:
            try:
                h = hash_file(path)
                duplicates[h].append((path, size))
                hashed_files += 1
            except Exception as e:
                print(f"[!] hash 失败 {path}: {e}")

    print(f"[*] 已 hash {hashed_files} 个候选文件")

    # 只保留有重复的
    real_dupes = {h: paths for h, paths in duplicates.items() if len(paths) > 1}

    print(f"\n📊 找到 {len(real_dupes)} 组重复文件")

    total_waste = 0
    for h, paths in real_dupes.items():
        size = paths[0][1]
        waste = size * (len(paths) - 1)
        total_waste += waste

    print(f"💾 重复占用空间: {human_size(total_waste)}\n")

    # 生成报告
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(f"# 微信附件去重报告\n\n")
        f.write(f"- 扫描目录: `{root}`\n")
        f.write(f"- 候选文件: {total_files}\n")
        f.write(f"- 重复组数: {len(real_dupes)}\n")
        f.write(f"- 浪费空间: **{human_size(total_waste)}**\n\n")

        # 按浪费空间排序
        sorted_dupes = sorted(
            real_dupes.items(),
            key=lambda kv: -kv[1][0][1] * (len(kv[1]) - 1),
        )

        for h, paths in sorted_dupes:
            size = paths[0][1]
            waste = size * (len(paths) - 1)
            f.write(f"## {paths[0][0].name} ({human_size(size)} × {len(paths)} 份, 浪费 {human_size(waste)})\n\n")
            f.write(f"`hash: {h[:16]}...`\n\n")
            for i, (p, _) in enumerate(paths):
                rel = p.relative_to(root)
                marker = "✅ 保留" if i == 0 else "🗑 重复"
                f.write(f"- {marker} `{rel}`\n")
            f.write("\n")

    print(f"📝 报告写入: {args.report}")

    # 实际去重
    if args.apply:
        print(f"\n[*] 开始执行 {args.link_mode}...")
        replaced = 0
        for h, paths in real_dupes.items():
            keep = paths[0][0]
            for p, _ in paths[1:]:
                try:
                    p.unlink()
                    if args.link_mode == "hardlink":
                        os.link(keep, p)
                    elif args.link_mode == "symlink":
                        os.symlink(keep, p)
                    # delete: 不创建链接
                    replaced += 1
                except Exception as e:
                    print(f"[!] 处理失败 {p}: {e}")
        print(f"[*] 完成: 处理了 {replaced} 个重复文件")
    else:
        print(f"\n💡 用 --apply 可执行实际去重")


if __name__ == "__main__":
    main()
