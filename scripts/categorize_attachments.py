"""把微信导出的附件按文件类型分类整理。

逻辑:
- 不动原文件,只生成"分类视图"(软链接)
- 在指定目录下建 documents/ images/ videos/ audios/ archives/ others/ 子目录
- 每个软链接指向原文件,在原会话目录里保留
- 这样既能"按会话查看",又能"按类型浏览"

Usage:
  # 默认: 在 微信渠道/_by_type/ 下建分类视图
  python3 scripts/categorize_attachments.py \
    --root "/path/to/Obsidian Vault/微信渠道"

  # 自定义输出目录
  python3 scripts/categorize_attachments.py \
    --root "/path/to/Obsidian Vault/微信渠道" \
    --out-dir "/path/to/Obsidian Vault/微信渠道/_分类视图"
"""
import argparse
import os
from pathlib import Path
from collections import defaultdict


CATEGORIES = {
    "documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".txt", ".csv", ".rtf", ".odt", ".ods", ".odp", ".key",
                  ".pages", ".numbers"},
    "images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
               ".tiff", ".heic", ".raw"},
    "videos": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"},
    "audios": {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus", ".amr"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "code": {".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp",
             ".go", ".rs", ".sh", ".json", ".xml", ".yaml", ".yml", ".md"},
    "ebooks": {".epub", ".mobi", ".azw3", ".djvu"},
}


def categorize(suffix: str) -> str:
    s = suffix.lower()
    for cat, exts in CATEGORIES.items():
        if s in exts:
            return cat
    return "others"


def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument(
        "--out-dir",
        default=None,
        help="分类视图输出目录(默认 root/_by_type)",
    )
    ap.add_argument(
        "--mode",
        choices=["symlink", "copy", "move"],
        default="symlink",
        help="symlink: 软链接(默认,不占空间) | copy: 复制 | move: 移动",
    )
    ap.add_argument("--report", default="/tmp/wechat_categorize_report.md")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    out_dir = Path(args.out_dir) if args.out_dir else (root / "_by_type")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 创建分类目录
    for cat in list(CATEGORIES.keys()) + ["others"]:
        (out_dir / cat).mkdir(exist_ok=True)

    print(f"[*] 扫描 {root}...")
    print(f"[*] 输出 {out_dir}")
    print(f"[*] 模式 {args.mode}")

    stats = defaultdict(lambda: {"count": 0, "size": 0})
    skipped_md = 0
    processed = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.is_symlink():
            continue
        # 跳过 .md(笔记)
        if path.suffix.lower() == ".md":
            skipped_md += 1
            continue
        # 跳过 _by_type 自己
        try:
            path.relative_to(out_dir)
            continue
        except ValueError:
            pass

        try:
            size = path.stat().st_size
        except OSError:
            continue

        cat = categorize(path.suffix)
        stats[cat]["count"] += 1
        stats[cat]["size"] += size

        # 生成目标路径(含会话子目录,避免重名)
        try:
            rel_parent = path.parent.relative_to(root)
            session = str(rel_parent).split("/")[0] if str(rel_parent) != "." else "root"
        except ValueError:
            session = "root"

        target_dir = out_dir / cat / session
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name

        # 如果重名,加序号
        if target.exists():
            base = target.stem
            ext = target.suffix
            i = 1
            while True:
                target = target_dir / f"{base}_{i}{ext}"
                if not target.exists():
                    break
                i += 1

        try:
            if args.mode == "symlink":
                os.symlink(path.resolve(), target)
            elif args.mode == "copy":
                import shutil
                shutil.copy2(path, target)
            elif args.mode == "move":
                import shutil
                shutil.move(str(path), str(target))
            processed += 1
        except Exception as e:
            print(f"[!] 处理失败 {path}: {e}")

    # 报告
    print(f"\n📊 分类统计:")
    print(f"{'类别':<15} {'文件数':<8} {'总大小':<12}")
    print("-" * 40)

    sorted_stats = sorted(stats.items(), key=lambda kv: -kv[1]["size"])
    for cat, info in sorted_stats:
        print(f"{cat:<15} {info['count']:<8} {human_size(info['size']):<12}")

    print(f"\n[*] 处理 {processed} 个文件")
    print(f"[*] 跳过 {skipped_md} 个 .md(笔记不算附件)")
    print(f"[*] 输出: {out_dir}")

    # 写报告
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(f"# 微信附件分类报告\n\n")
        f.write(f"- 扫描根: `{root}`\n")
        f.write(f"- 输出: `{out_dir}`\n")
        f.write(f"- 模式: `{args.mode}`\n")
        f.write(f"- 处理文件数: {processed}\n\n")
        f.write(f"| 类别 | 文件数 | 总大小 |\n|---|---|---|\n")
        for cat, info in sorted_stats:
            f.write(f"| {cat} | {info['count']} | {human_size(info['size'])} |\n")

    print(f"\n📝 报告: {args.report}")


if __name__ == "__main__":
    main()
