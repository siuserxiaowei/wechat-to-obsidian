#!/usr/bin/env python3
"""One-command pipeline: WeChat group -> Obsidian -> daily report HTML/PNG.

This script deliberately keeps WeChat reading local. GitHub Actions cannot read
your local WeChat database, so automation should run on the local Mac and then
optionally commit the generated report HTML/PNG to a GitHub Pages repository.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WECHAT2OBSIDIAN = ROOT / "scripts" / "wechat2obsidian.py"
DEFAULT_DAILY_REPORT_REPO = ROOT.parent / "wechat-daily-report-skill"
DEFAULT_WORK_DIR = ROOT / ".daily-pipeline"
PUBLIC_GROUP_LABELS = {
    "group-1": "一群",
    "group-2": "二群",
    "group-3": "三群",
    "group-4": "四群",
    "group-5": "五群",
}


def die(message: str) -> None:
    print(f"[!] {message}", file=sys.stderr)
    raise SystemExit(1)


def info(message: str) -> None:
    print(f"[*] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[!] {message}", file=sys.stderr, flush=True)


def expand(path: str | Path) -> Path:
    return Path(path).expanduser()


def repo_relative_path(value: str | Path) -> Path:
    path = expand(value)
    return path if path.is_absolute() else ROOT / path


def safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value, flags=re.UNICODE).strip("-_.")
    return cleaned or "wechat-group"


def parse_day(value: str | None) -> str:
    if not value or value == "today":
        return dt.date.today().isoformat()
    if value == "yesterday":
        return (dt.date.today() - dt.timedelta(days=1)).isoformat()
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        die(f"Invalid --date {value!r}; expected today, yesterday, or YYYY-MM-DD")


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    info("Running: " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, file=sys.stderr, end="")
        die(f"Command failed with exit code {result.returncode}: {' '.join(cmd)}")
    if result.stdout.strip():
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    return result


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_value(config: dict[str, Any], key: str, env_key: str, default_env: str = "") -> str:
    value = str(config.get(key) or "").strip()
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    if value:
        return value
    env_name = str(config.get(env_key) or default_env).strip()
    return os.environ.get(env_name, "") if env_name else ""


def post_json(url: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def load_config(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    if not path.exists():
        die(f"Config not found: {path}")
    data = load_json(path)
    if not isinstance(data, dict):
        die("Pipeline config root must be an object")
    return data


def load_env_file(path_value: str | None) -> None:
    if not path_value:
        return
    path = repo_relative_path(path_value)
    if not path.exists():
        warn(f"Env file not found, skipped: {path}")
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_groups(args: argparse.Namespace, config: dict[str, Any]) -> list[dict[str, Any]]:
    if args.chat or args.input_json:
        return [{
            "chat": args.chat,
            "title": args.title or args.chat or Path(args.input_json).stem,
            "subfolder": args.subfolder or safe_slug(args.chat or Path(args.input_json).stem),
            "folder": args.folder,
            "limit": args.limit,
            "binary": args.binary,
            "cli": args.cli,
            "media": args.media,
            "input_json": args.input_json,
        }]

    groups = config.get("groups") or []
    if not isinstance(groups, list) or not groups:
        die("No groups configured. Pass --chat, or create configs/group_daily.json with groups[].")
    return [item for item in groups if isinstance(item, dict)]


def merge_group(global_config: dict[str, Any], group: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    merged = dict(global_config)
    merged.update(group)
    if args.vault:
        merged["vault"] = args.vault
    if args.folder:
        merged["folder"] = args.folder
    if args.daily_report_repo:
        merged["daily_report_repo"] = args.daily_report_repo
    if args.publish_repo:
        merged.setdefault("publish", {})
        merged["publish"]["repo"] = args.publish_repo
    if args.publish_base_url:
        merged.setdefault("publish", {})
        merged["publish"]["base_url"] = args.publish_base_url
    if args.publish_push:
        merged.setdefault("publish", {})
        merged["publish"]["push"] = True
    if args.no_png:
        merged["no_png"] = True
    if args.ai_mode:
        merged["ai_mode"] = args.ai_mode
    return merged


def parse_simplified_lines(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("[") or "] " not in line:
            continue
        time_part, rest = line.split("] ", 1)
        segments = []
        for part in rest.split(" | "):
            if ":" in part:
                name, content = part.split(":", 1)
                segments.append({"name": name.strip(), "content": content.strip()})
        if segments:
            rows.append({"time": time_part.strip("[]"), "segments": segments, "raw": rest})
    return rows


def heuristic_ai_content(stats: dict[str, Any], simplified_text: str) -> dict[str, Any]:
    """Create a deterministic fallback ai_content.json.

    Daily automation can later replace this with a model-generated JSON, but
    this keeps the pipeline useful even without API keys.
    """
    meta = stats.get("meta", {})
    top_words = [item.get("text") for item in stats.get("word_cloud", [])[:10] if item.get("text")]
    top_talkers = stats.get("top_talkers", [])
    rows = parse_simplified_lines(simplified_text)

    topics = []
    chunks = [top_words[i:i + 3] for i in range(0, min(len(top_words), 12), 3)] or [["群聊", "资料", "讨论"]]
    for index, words in enumerate(chunks[:4], 1):
        title = " / ".join(words)
        topics.append({
            "title": f"今日话题 {index}: {title}",
            "category": "群聊干货",
            "summary": f"今天围绕 {title} 有多轮讨论。建议回看原始聊天，把其中的资源、观点和行动项继续沉淀到 Obsidian。",
            "keywords": words,
            "mention_count": sum(item.get("count", 0) for item in stats.get("word_cloud", []) if item.get("text") in words),
        })

    url_re = re.compile(r"https?://[^\s)）]+")
    resources = []
    for row in rows:
        for seg in row["segments"]:
            for url in url_re.findall(seg["content"]):
                resources.append({
                    "type": "链接",
                    "title": url[:80],
                    "sharer": seg["name"],
                    "time": row["time"].split("~")[0],
                    "category": "资料",
                    "description": "群聊中出现的链接，建议打开确认价值并整理到主题笔记。",
                    "key_points": ["保留原始上下文", "确认链接内容", "按主题归档"],
                    "url": url,
                })
    resources = resources[:8]

    important_messages = []
    important_keywords = ("推荐", "必须", "记得", "关键", "干货", "资料", "教程", "链接", "总结", "方法", "机会")
    for row in rows:
        for seg in row["segments"]:
            content = seg["content"]
            if any(word in content for word in important_keywords) or len(content) >= 80:
                important_messages.append({
                    "priority": "中",
                    "sender": seg["name"],
                    "time": row["time"].split("~")[0],
                    "summary": content[:48],
                    "content": content,
                })
    important_messages = important_messages[:8]

    dialogues = []
    for row in rows[:4]:
        messages = []
        for seg in row["segments"][:6]:
            messages.append({
                "name": seg["name"],
                "time": row["time"].split("~")[0],
                "content": seg["content"],
            })
        if len(messages) >= 2:
            dialogues.append({
                "topic": "聊天现场回放",
                "messages": messages,
                "highlight": "这段对话值得回看，建议结合上下文再提炼成主题笔记。",
            })

    qas = []
    for row in rows:
        question = None
        answer = None
        for seg in row["segments"]:
            if question is None and ("?" in seg["content"] or "？" in seg["content"] or seg["content"].startswith(("怎么", "为什么", "能不能"))):
                question = seg
            elif question and seg["name"] != question["name"]:
                answer = seg
                break
        if question and answer:
            qas.append({
                "questioner": question["name"],
                "question_time": row["time"].split("~")[0],
                "question": question["content"],
                "tags": ["群聊问答"],
                "answerer": answer["name"],
                "answer_time": row["time"].split("~")[-1],
                "answer": answer["content"],
                "is_best": True,
            })
        if len(qas) >= 3:
            break

    profiles = {}
    for talker in top_talkers:
        words = talker.get("common_words") or []
        traits = ["高频发言"]
        if words:
            traits.append("关注 " + " / ".join(words[:2]))
        traits.append(f"{talker.get('count', 0)} 条消息")
        profiles[talker.get("name", "unknown")] = {"traits": traits[:3]}

    return {
        "topics": topics,
        "resources": resources,
        "important_messages": important_messages,
        "dialogues": dialogues,
        "qas": qas,
        "talker_profiles": profiles,
        "pipeline_note": "heuristic ai_content generated locally; replace with model-generated JSON for deeper analysis.",
    }


def write_analysis_note(path: Path, title: str, day: str, ai_content: dict[str, Any], report_html: Path, report_png: Path | None) -> None:
    lines = [
        "---",
        "source: group-daily-pipeline",
        f"title: {json.dumps(title + ' 干货分析', ensure_ascii=False)}",
        f"date: {json.dumps(day, ensure_ascii=False)}",
        "---",
        "",
        f"# {day} · {title} 干货分析",
        "",
        f"- 日报 HTML: [[{report_html.name}]]",
    ]
    if report_png:
        lines.append(f"- 日报长图: ![[{report_png.name}]]")
    lines.extend(["", "## 核心话题", ""])
    for topic in ai_content.get("topics", []):
        lines.append(f"### {topic.get('title', '未命名话题')}")
        lines.append(str(topic.get("summary", "")))
        lines.append("")
    lines.extend(["## 资料与链接", ""])
    resources = ai_content.get("resources", [])
    if not resources:
        lines.append("- 暂未识别到明确链接，建议回看原始记录。")
    for res in resources:
        url = res.get("url") or ""
        title_text = res.get("title") or url or "资源"
        lines.append(f"- {title_text}" + (f": {url}" if url else ""))
    lines.extend(["", "## 重要消息", ""])
    for msg in ai_content.get("important_messages", []):
        lines.append(f"- **{msg.get('sender', '')} {msg.get('time', '')}**: {msg.get('content') or msg.get('summary', '')}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def public_label_from_slug(slug: str) -> str:
    if slug in PUBLIC_GROUP_LABELS:
        return PUBLIC_GROUP_LABELS[slug]
    if slug.startswith("group-"):
        return "演示群"
    return "匿名群"


def render_public_demo_report(title: str, day: str) -> str:
    safe_title = html_lib.escape(title)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}日报演示 · {day}</title>
  <style>
    :root {{ color-scheme: light; --ink: #18231f; --muted: #66736d; --line: #dbe5dc; --paper: #fbfaf5; --panel: #ffffff; --accent: #b95f2b; --accent-soft: #f5e7dc; --green: #2b6d58; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif; color: var(--ink); background: linear-gradient(135deg, #eef4ec 0%, var(--paper) 48%, #eef2f6 100%); }}
    main {{ width: min(1060px, calc(100vw - 48px)); margin: 0 auto; padding: 72px 0 88px; }}
    .eyebrow {{ color: var(--accent); font-size: 13px; font-weight: 800; letter-spacing: 0; text-transform: uppercase; }}
    .hero {{ margin-top: 38px; padding: 34px; border-radius: 8px; color: #fff9ed; background: linear-gradient(135deg, rgba(31, 43, 36, .96), rgba(54, 73, 84, .92), rgba(116, 55, 35, .88)); }}
    h1 {{ margin: 10px 0 14px; font-size: clamp(42px, 7vw, 76px); line-height: 1; letter-spacing: 0; }}
    .hero p {{ width: min(760px, 100%); color: rgba(255, 249, 237, .82); font-size: 18px; line-height: 1.8; }}
    .hero-grid, .cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 30px; }}
    .hero-metric {{ padding: 18px; border: 1px solid rgba(255,255,255,.2); border-radius: 8px; background: rgba(255,255,255,.08); }}
    .hero-metric strong {{ display: block; font-size: 34px; }}
    .hero-metric span {{ display: block; margin-top: 8px; color: rgba(255,249,237,.72); font-size: 14px; }}
    .section-title {{ margin: 46px 0 16px; font-size: 32px; letter-spacing: 0; }}
    .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 0; }}
    .card, .notice {{ border: 1px solid var(--line); border-radius: 8px; background: rgba(255, 255, 255, .78); box-shadow: 0 18px 50px rgba(36, 48, 40, .06); }}
    .card {{ padding: 24px; min-height: 150px; }}
    .card h2 {{ margin: 0 0 12px; font-size: 22px; letter-spacing: 0; }}
    .card p {{ margin: 0; color: var(--muted); line-height: 1.75; }}
    .notice {{ margin-top: 32px; padding: 18px 20px; color: var(--green); background: #eef6ef; }}
    @media (max-width: 720px) {{ main {{ width: min(100vw - 28px, 1060px); padding-top: 36px; }} .hero-grid, .cards {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <div class="eyebrow">Wechat Daily Demo · {day}</div>
    <section class="hero">
      <div class="eyebrow">匿名群日报</div>
      <h1>{safe_title}日报</h1>
      <p>公开页面只展示日报样式和信息组织方式。真实群名、成员昵称、头像、聊天原文、链接、词云和具体讨论内容不会出现在这里。</p>
      <div class="hero-grid">
        <div class="hero-metric"><strong>已生成</strong><span>日报状态</span></div>
        <div class="hero-metric"><strong>已归档</strong><span>Obsidian 私有库</span></div>
        <div class="hero-metric"><strong>已发布</strong><span>GitHub Pages 演示</span></div>
      </div>
    </section>
    <h2 class="section-title">日报结构演示</h2>
    <section class="cards">
      <article class="card"><h2>今日概览</h2><p>展示当天讨论是否活跃、是否有值得复盘的内容，以及是否已经写入本地知识库。</p></article>
      <article class="card"><h2>话题提炼</h2><p>把碎片化聊天归纳成几个主题，方便后续在 Obsidian 里继续整理成长期笔记。</p></article>
      <article class="card"><h2>资料收纳</h2><p>识别链接、文件、工具和案例，但公开演示页不会展示真实链接或原始分享人。</p></article>
      <article class="card"><h2>行动项</h2><p>把需要跟进的想法、问题和待办沉淀下来，第二天可以继续追踪。</p></article>
    </section>
    <div class="notice">隐私说明：完整聊天记录和详细分析只保存在本地 Obsidian；公开页面仅用于展示产品效果。</div>
  </main>
</body>
</html>
"""


def update_publish_index(publish_repo: Path, base_url: str | None, public_demo: bool = True) -> None:
    reports = sorted((publish_repo / "reports").glob("*/*/index.html"), reverse=True)
    items = []
    for html in reports[:80]:
        rel = html.relative_to(publish_repo)
        slug = rel.parts[1]
        day = rel.parts[2]
        if public_demo and not slug.startswith("group-"):
            continue
        label = public_label_from_slug(slug) if public_demo else f"{slug} / {day}"
        href = rel.as_posix()
        items.append(
            f'<a class="report-card" href="{html_lib.escape(href)}">'
            f'<span class="slug">{html_lib.escape(slug)}</span>'
            f'<span class="day">{html_lib.escape(day)}</span>'
            f'<span class="arrow">open</span>'
            f'</a>'
        )
    html_text = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>微信群日报演示</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #18231f;
      --muted: #64716b;
      --line: #dbe5dc;
      --paper: #fbfaf5;
      --panel: #ffffff;
      --accent: #b95f2b;
      --accent-soft: #f5e7dc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif;
      background: linear-gradient(135deg, #eef4ec 0%, var(--paper) 48%, #eef2f6 100%);
      color: var(--ink);
    }
    main {
      width: min(1040px, calc(100vw - 48px));
      margin: 0 auto;
      padding: 72px 0 88px;
    }
    .eyebrow {
      color: var(--accent);
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    h1 {
      margin: 12px 0 12px;
      font-size: clamp(42px, 8vw, 88px);
      line-height: .96;
      letter-spacing: 0;
    }
    .lead {
      width: min(720px, 100%);
      margin: 0;
      color: var(--muted);
      font-size: 18px;
      line-height: 1.8;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin: 44px 0 28px;
    }
    .metric {
      padding: 22px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, .68);
      border-radius: 8px;
    }
    .metric strong {
      display: block;
      font-size: 32px;
      line-height: 1;
    }
    .metric span {
      display: block;
      margin-top: 10px;
      color: var(--muted);
      font-size: 14px;
    }
    .grid {
      display: grid;
      gap: 12px;
      margin-top: 18px;
    }
    .report-card {
      display: grid;
      grid-template-columns: 1fr auto auto;
      align-items: center;
      gap: 16px;
      padding: 20px 22px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: inherit;
      text-decoration: none;
      box-shadow: 0 18px 50px rgba(36, 48, 40, .06);
    }
    .report-card:hover {
      border-color: #c9d7cd;
      transform: translateY(-1px);
    }
    .slug {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 18px;
      font-weight: 800;
    }
    .day {
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }
    .arrow {
      padding: 8px 11px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }
    @media (max-width: 720px) {
      main { width: min(100vw - 28px, 1040px); padding-top: 36px; }
      .summary { grid-template-columns: 1fr; }
      .report-card { grid-template-columns: 1fr; align-items: start; }
      .slug { white-space: normal; }
      h1 { font-size: 46px; }
    }
  </style>
</head>
<body>
  <main>
    <div class="eyebrow">Wechat Daily Archive</div>
    <h1>微信群日报演示</h1>
    <p class="lead">这是对外演示页，只展示日报产品形态。真实群名、成员昵称、聊天原文、链接、头像和敏感统计都已隐藏。</p>
    <section class="summary">
      <div class="metric"><strong>""" + str(len(items)) + """</strong><span>演示群组</span></div>
      <div class="metric"><strong>08:30</strong><span>每天自动运行</span></div>
      <div class="metric"><strong>0</strong><span>公开聊天原文</span></div>
    </section>
    <section class="grid">
""" + "\n".join(items) + """
    </section>
  </main>
</body>
</html>
"""
    (publish_repo / "index.html").write_text(html_text, encoding="utf-8")


def publish_report(
    publish: dict[str, Any],
    slug: str,
    day: str,
    html_path: Path,
    png_path: Path | None,
    public_title: str | None = None,
    public_slug: str | None = None,
) -> str:
    repo = repo_relative_path(str(publish.get("repo", "")))
    if not repo:
        return ""
    if not repo.exists():
        die(f"Publish repo not found: {repo}")
    privacy = str(publish.get("privacy") or publish.get("public_mode") or "demo")
    public_demo = privacy != "full"
    target_slug = safe_slug(public_slug or slug) if public_demo else slug
    title = public_title or public_label_from_slug(target_slug)
    dest = repo / "reports" / target_slug / day
    dest.mkdir(parents=True, exist_ok=True)
    if public_demo:
        (dest / "index.html").write_text(render_public_demo_report(title, day), encoding="utf-8")
    else:
        shutil.copy2(html_path, dest / "index.html")
    if not public_demo and png_path and png_path.exists():
        shutil.copy2(png_path, dest / "report.png")
    update_publish_index(repo, publish.get("base_url"), public_demo=public_demo)

    if publish.get("push"):
        run(["git", "add", "index.html", "reports"], cwd=repo)
        status = subprocess.run(["git", "status", "--short"], cwd=repo, text=True, capture_output=True)
        if status.stdout.strip():
            run(["git", "commit", "-m", f"Add {day} {slug} daily report"], cwd=repo)
            run(["git", "push"], cwd=repo)
    base_url = str(publish.get("base_url") or "").rstrip("/")
    return f"{base_url}/reports/{target_slug}/{day}/" if base_url else str(dest / "index.html")


def notification_text(day: str, summaries: list[dict[str, Any]]) -> str:
    lines = [
        f"微信群日报已生成: {day}",
        "",
        f"群数量: {len(summaries)}",
    ]
    for index, item in enumerate(summaries, 1):
        count = item.get("message_count", 0)
        active = item.get("active_user_count", 0)
        time_range = item.get("time_range") or "无活跃时段"
        link = item.get("publish_url") or item.get("report_html") or ""
        lines.extend([
            "",
            f"{index}. {item.get('title', '未命名群')}",
            f"消息: {count} 条 / 活跃成员: {active} 人 / 时段: {time_range}",
            f"日报: {link}",
        ])
    return "\n".join(lines)


def send_telegram(telegram: dict[str, Any], text: str) -> None:
    if not telegram.get("enabled"):
        return
    token = resolve_value(telegram, "bot_token", "bot_token_env", "TELEGRAM_BOT_TOKEN")
    chat_id = resolve_value(telegram, "chat_id", "chat_id_env", "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        warn("Telegram notify skipped: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or fill notify.telegram in config.")
        return
    try:
        post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:3900],
                "disable_web_page_preview": False,
            },
        )
        info("Telegram notification sent")
    except (urllib.error.URLError, TimeoutError) as exc:
        warn(f"Telegram notify failed: {exc}")


def feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_feishu(feishu: dict[str, Any], text: str) -> None:
    if not feishu.get("enabled"):
        return
    webhook_url = resolve_value(feishu, "webhook_url", "webhook_url_env", "FEISHU_WEBHOOK_URL")
    secret = resolve_value(feishu, "secret", "secret_env", "FEISHU_WEBHOOK_SECRET")
    if not webhook_url:
        warn("Feishu notify skipped: set FEISHU_WEBHOOK_URL, or fill notify.feishu.webhook_url in config.")
        return

    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text},
    }
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)

    try:
        post_json(webhook_url, payload)
        info("Feishu notification sent")
    except (urllib.error.URLError, TimeoutError) as exc:
        warn(f"Feishu notify failed: {exc}")


def send_notifications(config: dict[str, Any], day: str, summaries: list[dict[str, Any]]) -> None:
    notify = config.get("notify")
    if not isinstance(notify, dict):
        return
    text = notification_text(day, summaries)
    telegram = notify.get("telegram")
    if isinstance(telegram, dict):
        send_telegram(telegram, text)
    feishu = notify.get("feishu")
    if isinstance(feishu, dict):
        send_feishu(feishu, text)


def run_group(day: str, group: dict[str, Any], args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    chat = str(group.get("chat") or "")
    input_json = group.get("input_json")
    if not chat and not input_json:
        die("Each group needs chat or input_json")

    title = str(group.get("title") or chat or Path(str(input_json)).stem)
    slug = safe_slug(str(group.get("slug") or title))
    vault = expand(str(group.get("vault") or ""))
    if not vault.exists():
        die(f"Obsidian vault not found: {vault}")

    folder = str(group.get("folder") or "微信渠道")
    subfolder = str(group.get("subfolder") or f"付费群/{title}")
    month = day[:7]
    obsidian_dir = vault / folder / subfolder / month
    obsidian_dir.mkdir(parents=True, exist_ok=True)

    daily_repo = repo_relative_path(str(group.get("daily_report_repo") or DEFAULT_DAILY_REPORT_REPO))
    if not (daily_repo / "scripts" / "generate_report.py").exists():
        die(f"Daily report repo not found or incomplete: {daily_repo}")

    work_root = repo_relative_path(str(group.get("work_dir") or DEFAULT_WORK_DIR)) / day / slug
    work_root.mkdir(parents=True, exist_ok=True)
    raw_json = work_root / f"{day}-wx-history.json"
    stats_json = work_root / "stats.json"
    simplified_txt = work_root / "simplified_chat.txt"
    ai_content_json = work_root / "ai_content.json"
    report_html_work = work_root / f"{day}-日报.html"
    report_png_work = work_root / f"{day}-日报.png"

    import_cmd = [
        sys.executable,
        str(WECHAT2OBSIDIAN),
        "import-wx-cli",
        "--vault",
        str(vault),
        "--folder",
        folder,
        "--subfolder",
        subfolder,
        "--title",
        title,
        "--since",
        day,
        "--until",
        day,
        "--limit",
        str(group.get("limit") or 5000),
        "--mode",
        str(group.get("mode") or "overwrite"),
        "--json",
    ]
    if input_json:
        import_cmd.extend(["--input-json", str(expand(str(input_json)))])
        shutil.copy2(expand(str(input_json)), raw_json)
    else:
        import_cmd.extend(["--chat", chat, "--raw-output", str(raw_json)])
        if group.get("binary"):
            import_cmd.extend(["--binary", str(expand(str(group["binary"])))])
        if group.get("cli"):
            import_cmd.extend(["--cli", str(group["cli"])])
        if group.get("media"):
            import_cmd.append("--media")
    if group.get("no_media_copy"):
        import_cmd.append("--no-media-copy")
    run(import_cmd, cwd=ROOT)

    run([
        sys.executable,
        str(daily_repo / "scripts" / "wx_cli_to_report.py"),
        "--input-json",
        str(raw_json),
        "--chatroom",
        title,
        "--date",
        day,
        "--output-stats",
        str(stats_json),
        "--output-text",
        str(simplified_txt),
    ], cwd=daily_repo)

    stats_for_summary = load_json(stats_json)
    ai_mode = str(group.get("ai_mode") or "heuristic")
    if group.get("ai_content"):
        shutil.copy2(expand(str(group["ai_content"])), ai_content_json)
    elif ai_mode == "heuristic":
        simplified = simplified_txt.read_text(encoding="utf-8")
        write_json(ai_content_json, heuristic_ai_content(stats_for_summary, simplified))
    else:
        die(f"Unsupported ai_mode={ai_mode!r}. Use heuristic or provide ai_content.")

    run([
        sys.executable,
        str(daily_repo / "scripts" / "generate_report.py"),
        "--stats",
        str(stats_json),
        "--ai-content",
        str(ai_content_json),
        "--output",
        str(report_html_work),
    ], cwd=daily_repo)

    report_png_final: Path | None = None
    if not group.get("no_png"):
        run([
            sys.executable,
            str(daily_repo / "scripts" / "generate_report.py"),
            "--stats",
            str(stats_json),
            "--ai-content",
            str(ai_content_json),
            "--output",
            str(report_png_work),
            "--viewport-width",
            str(group.get("viewport_width") or 1180),
            "--viewport-height",
            str(group.get("viewport_height") or 1400),
            "--device-scale-factor",
            str(group.get("device_scale_factor") or 2),
        ], cwd=daily_repo)

    report_html_final = obsidian_dir / f"{day}-日报.html"
    shutil.copy2(report_html_work, report_html_final)
    if report_png_work.exists():
        report_png_final = obsidian_dir / f"{day}-日报.png"
        shutil.copy2(report_png_work, report_png_final)
    shutil.copy2(stats_json, obsidian_dir / f"{day}-stats.json")
    shutil.copy2(ai_content_json, obsidian_dir / f"{day}-ai_content.json")
    raw_dir = obsidian_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(raw_json, raw_dir / f"{day}-wx-history.json")
    write_analysis_note(
        obsidian_dir / f"{day}-干货分析.md",
        title,
        day,
        load_json(ai_content_json),
        report_html_final,
        report_png_final,
    )

    publish_url = ""
    publish = group.get("publish")
    if isinstance(publish, dict) and publish.get("repo"):
        publish_url = publish_report(
            publish,
            slug,
            day,
            report_html_work,
            report_png_work if report_png_work.exists() else None,
            public_title=group.get("public_title"),
            public_slug=group.get("public_slug"),
        )

    return {
        "chat": chat or str(input_json),
        "title": title,
        "date": day,
        "message_count": stats_for_summary.get("meta", {}).get("total_count", 0),
        "active_user_count": stats_for_summary.get("meta", {}).get("active_user_count", 0),
        "time_range": stats_for_summary.get("meta", {}).get("time_range", ""),
        "obsidian_dir": str(obsidian_dir),
        "report_html": str(report_html_final),
        "report_png": str(report_png_final) if report_png_final else "",
        "publish_url": publish_url,
        "work_dir": str(work_root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run WeChat group -> Obsidian -> daily report pipeline")
    parser.add_argument("--config", help="JSON config path. See configs/group_daily.example.json")
    parser.add_argument("--chat", help="Single chat/group name or id")
    parser.add_argument("--title", help="Single chat display title")
    parser.add_argument("--input-json", help="Use existing wx-cli history JSON for a single run")
    parser.add_argument("--date", default="yesterday", help="today, yesterday, or YYYY-MM-DD")
    parser.add_argument("--vault", help="Obsidian vault root")
    parser.add_argument("--folder", help="Folder inside the vault")
    parser.add_argument("--subfolder", help="Subfolder for a single chat")
    parser.add_argument("--limit", type=int, default=5000, help="Message limit")
    parser.add_argument("--cli", choices=["auto", "wx", "wechat-cli"], default="auto", help="WeChat CLI command")
    parser.add_argument("--binary", help="Explicit wx/wechat-cli binary path")
    parser.add_argument("--media", action="store_true", help="Ask CLI to resolve media when supported")
    parser.add_argument("--daily-report-repo", help="Path to wechat-daily-report-skill repo")
    parser.add_argument("--publish-repo", help="Optional GitHub Pages repo path to copy reports into")
    parser.add_argument("--publish-base-url", help="Optional GitHub Pages base URL")
    parser.add_argument("--publish-push", action="store_true", help="Commit and push copied reports")
    parser.add_argument("--ai-mode", choices=["heuristic"], help="AI content mode")
    parser.add_argument("--no-png", action="store_true", help="Skip PNG rendering")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    day = parse_day(args.date)
    config = load_config(expand(args.config) if args.config else None)
    load_env_file(config.get("env_file"))
    groups = normalize_groups(args, config)
    summaries = []
    for group in groups:
        merged = merge_group(config, group, args)
        summaries.append(run_group(day, merged, args, config))
    send_notifications(config, day, summaries)
    if args.json:
        print(json.dumps({"date": day, "reports": summaries}, ensure_ascii=False, indent=2))
    else:
        info("Daily pipeline complete")
        for item in summaries:
            info(f"{item['title']}: {item['report_html']}")
            if item.get("publish_url"):
                info(f"published: {item['publish_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
