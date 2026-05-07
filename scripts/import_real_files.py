"""导入微信里**真实的用户文件**(PDF/Word/Excel/视频/...) 到 Obsidian vault。

WeChat 文件分布:
- msg/attach/ : 加密的 .dat 图片(export_chat.py 已经处理)
- msg/file/   : ⭐ 真实用户文件(PDF/DOCX/XLSX/...) ← 这个脚本处理
- msg/video/  : ⭐ 视频文件 ← 这个脚本处理

导入策略:
- 默认硬链接(节省空间,不占额外硬盘)
- 按月份组织(微信原本就是按月分的)
- 同时建一个"按类型"的视图

Usage:
  python3 scripts/import_real_files.py \
    --wx-user-dir ~/Library/Containers/.../xwechat_files/wxid_xxx \
    --vault ~/Documents/Obsidian\\ Vault \
    --folder 微信渠道/_文件库 \
    --link-mode hardlink
"""
import argparse
import os
import shutil
from pathlib import Path
from collections import defaultdict


# 哪些扩展名归哪类
CATEGORIES = {
    "documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".txt", ".csv", ".rtf", ".odt", ".ods", ".odp", ".key",
                  ".pages", ".numbers", ".md", ".html", ".htm"},
    "videos": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"},
    "audios": {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus", ".amr"},
    "images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
               ".tiff", ".heic"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "ebooks": {".epub", ".mobi", ".azw3", ".djvu"},
    "code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs",
             ".sh", ".json", ".xml", ".yaml", ".yml", ".sql", ".skill"},
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


def safe_name(name: str) -> str:
    safe = name
    for c in '/:*?"<>|':
        safe = safe.replace(c, "_")
    return safe.strip()[:200] or "unnamed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--wx-user-dir",
        required=True,
        help="WeChat 的某个用户目录,例如 .../xwechat_files/wxid_xxx_xxxx",
    )
    ap.add_argument("--vault", required=True, help="Obsidian vault 根")
    ap.add_argument(
        "--folder",
        default="微信渠道/_文件库",
        help="vault 内的目标子目录",
    )
    ap.add_argument(
        "--link-mode",
        choices=["hardlink", "symlink", "copy"],
        default="hardlink",
        help="hardlink: 硬链接(默认,不占额外空间) | symlink: 软链接 | copy: 复制",
    )
    ap.add_argument(
        "--by-month",
        action="store_true",
        default=True,
        help="按月份子目录组织(默认开)",
    )
    ap.add_argument(
        "--by-type",
        action="store_true",
        default=True,
        help="额外建一个按类型的索引视图(默认开)",
    )
    ap.add_argument(
        "--include-attach",
        action="store_true",
        help="也导入 msg/attach/ 的 .dat 文件(一般不用)",
    )
    ap.add_argument("--report", default="/tmp/wechat_import_report.md")
    args = ap.parse_args()

    wx_user = Path(args.wx_user_dir).expanduser()
    vault_dir = Path(args.vault).expanduser() / args.folder
    vault_dir.mkdir(parents=True, exist_ok=True)

    # 要扫描的源目录
    sources = [
        ("msg/file", "📄 文档库"),
        ("msg/video", "🎬 视频库"),
    ]
    if args.include_attach:
        sources.append(("msg/attach", "🖼 图片附件"))

    by_month_dir = vault_dir / "01.按月份"
    by_type_dir = vault_dir / "02.按类型"
    by_month_dir.mkdir(exist_ok=True)
    by_type_dir.mkdir(exist_ok=True)

    stats = defaultdict(lambda: {"count": 0, "size": 0})
    total_processed = 0
    total_skipped = 0
    failed = []

    for src_rel, label in sources:
        src_dir = wx_user / src_rel
        if not src_dir.exists():
            print(f"[!] 源目录不存在: {src_dir}")
            continue

        print(f"\n{label} ← {src_dir}")

        for path in src_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue

            try:
                # 月份目录(2024-09 这种)
                rel_to_src = path.relative_to(src_dir)
                parts = rel_to_src.parts
                month = parts[0] if len(parts) > 0 and "-" in parts[0] else "unsorted"
                file_name = safe_name(path.name)

                size = path.stat().st_size
                cat = categorize(path.suffix)
                stats[cat]["count"] += 1
                stats[cat]["size"] += size

                # 1) 按月份放
                month_target_dir = by_month_dir / month
                month_target_dir.mkdir(exist_ok=True)
                target = month_target_dir / file_name

                if target.exists():
                    # 同名,加序号
                    base, ext = target.stem, target.suffix
                    i = 1
                    while True:
                        target = month_target_dir / f"{base}_dup{i}{ext}"
                        if not target.exists():
                            break
                        i += 1

                if args.link_mode == "hardlink":
                    try:
                        os.link(path, target)
                    except OSError:
                        # 跨文件系统硬链接失败 → 退化成 copy
                        shutil.copy2(path, target)
                elif args.link_mode == "symlink":
                    os.symlink(path.resolve(), target)
                elif args.link_mode == "copy":
                    shutil.copy2(path, target)

                # 2) 按类型也建软链接(指向月份目录里的文件)
                if args.by_type:
                    type_target_dir = by_type_dir / cat
                    type_target_dir.mkdir(exist_ok=True)
                    type_target = type_target_dir / target.name
                    if not type_target.exists():
                        try:
                            os.symlink(target.resolve(), type_target)
                        except OSError:
                            pass

                total_processed += 1

            except FileExistsError:
                total_skipped += 1
            except Exception as e:
                failed.append((str(path), str(e)))

    # 报告
    print(f"\n{'='*60}")
    print(f"📊 完成")
    print(f"  处理 : {total_processed} 个文件")
    print(f"  跳过 : {total_skipped} 个(已存在)")
    print(f"  失败 : {len(failed)}")
    print(f"\n按类型统计:")
    print(f"{'类别':<15} {'文件数':<8} {'总大小':<12}")
    print("-" * 40)
    sorted_stats = sorted(stats.items(), key=lambda kv: -kv[1]["size"])
    for cat, info in sorted_stats:
        print(f"{cat:<15} {info['count']:<8} {human_size(info['size']):<12}")

    # 写报告
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(f"# 微信真实文件导入报告\n\n")
        f.write(f"- 源: `{wx_user}`\n")
        f.write(f"- 目标: `{vault_dir}`\n")
        f.write(f"- 模式: `{args.link_mode}`\n")
        f.write(f"- 处理: {total_processed} 个 / 跳过: {total_skipped} / 失败: {len(failed)}\n\n")
        f.write(f"## 按类型分布\n\n| 类别 | 文件数 | 总大小 |\n|---|---|---|\n")
        for cat, info in sorted_stats:
            f.write(f"| {cat} | {info['count']} | {human_size(info['size'])} |\n")
        if failed:
            f.write(f"\n## 失败列表(前 50)\n\n")
            for p, err in failed[:50]:
                f.write(f"- `{p}`: {err}\n")

    print(f"\n📝 报告: {args.report}")
    print(f"📁 文件库: {vault_dir}")


if __name__ == "__main__":
    main()
