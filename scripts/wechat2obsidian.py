#!/usr/bin/env python3
"""WeChat for macOS 4.x to Obsidian CLI.

This tool captures SQLCipher keys from a user's own local WeChat process,
decrypts WeChat databases, and exports conversation tables to daily Obsidian
Markdown files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


PAGE_SIZE = 4096
RESERVE = 80
IV_LEN = 16
SQLITE_HEADER = b"SQLite format 3\x00"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "wechat-to-obsidian"
DEFAULT_KEYS_LOG = DEFAULT_CACHE_DIR / "keys.log"
DEFAULT_WEFLOW_CONFIG = Path.home() / "Library" / "Application Support" / "weflow" / "WeFlow-config.json"
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
WECHAT_KEY_DATABASES = {
    "message_0.db": Path("db_storage/message/message_0.db"),
    "contact.db": Path("db_storage/contact/contact.db"),
    "favorite.db": Path("db_storage/favorite/favorite.db"),
    "session.db": Path("db_storage/session/session.db"),
}

TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    48: "location",
    49: "share",
    10000: "system",
    65537: "system_notice",
}

JS_HOOK = r"""
function buf2hex(buffer) {
    var a = new Uint8Array(buffer);
    var h = "";
    for (var i = 0; i < a.length; i++) {
        h += ("0" + a[i].toString(16)).slice(-2);
    }
    return h;
}

var LOG_PATH = LOG_PATH_PLACEHOLDER;
var found = false;

Process.enumerateModules().forEach(function(m) {
    if (found) return;
    m.enumerateExports().forEach(function(exp) {
        if (found) return;
        if (exp.name === "CCKeyDerivationPBKDF") {
            found = true;
            send("[*] Hook installed on " + m.name);
            Interceptor.attach(exp.address, {
                onEnter: function(args) {
                    this.pwLen = args[2].toInt32();
                    this.saltLen = args[4].toInt32();
                    this.rounds = args[6].toInt32();
                    this.salt = args[3];
                    this.dk = args[7];
                    this.dkLen = args[8].toInt32();
                },
                onLeave: function(retval) {
                    if (this.rounds !== 256000) return;
                    if (this.dkLen !== 32) return;
                    if (this.saltLen < 16 || this.saltLen > 64) return;
                    if (this.pwLen < 4 || this.pwLen > 256) return;

                    var saltHex = buf2hex(this.salt.readByteArray(this.saltLen));
                    var dkHex = buf2hex(this.dk.readByteArray(this.dkLen));
                    var f = new File(LOG_PATH, "a");
                    f.write("captured_at=" + (new Date()).toISOString() + "\n");
                    f.write("rounds=" + this.rounds + "\n");
                    f.write("salt=" + saltHex + "\n");
                    f.write("dk=" + dkHex + "\n\n");
                    f.flush();
                    f.close();
                    send("[PBKDF2] salt=" + saltHex.slice(0, 16) + "... dk=" + dkHex.slice(0, 12) + "...");
                }
            });
        }
    });
});

if (!found) send("[!] CCKeyDerivationPBKDF not found");
"""


def info(message: str) -> None:
    print(f"[*] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[!] {message}", file=sys.stderr, flush=True)


def die(message: str, code: int = 1) -> None:
    print(f"[!] {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def expand_path(path: str | Path) -> Path:
    return Path(path).expanduser()


def ensure_private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)


def run_checked(cmd: list[str]) -> None:
    info("Running: " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def infer_wx_cli_kind(binary: str) -> str:
    name = Path(binary).name
    return "wx" if name == "wx" or name.startswith("wx.") else "wechat-cli"


def resolve_wx_cli(cli: str = "auto", binary: str | None = None) -> tuple[str, str]:
    if binary:
        path = expand_path(binary)
        if not path.exists():
            die(f"wx-cli binary not found: {path}")
        return infer_wx_cli_kind(str(path)), str(path)

    if cli != "auto":
        found = shutil.which(cli)
        if not found:
            die(f"Command not found: {cli}")
        return cli, found

    for candidate in ("wx", "wechat-cli"):
        found = shutil.which(candidate)
        if found:
            return candidate, found
    die("Command not found: wx or wechat-cli. Install @jackwener/wx-cli, or pass --binary to the local wechat-cli package.")


def find_wechat_app() -> Path | None:
    for candidate in (
        Path("/Applications/WeChat.app"),
        Path.home() / "Applications" / "WeChat.app",
    ):
        if candidate.exists():
            return candidate

    if platform.system() == "Darwin" and command_exists("mdfind"):
        try:
            result = subprocess.run(
                ["mdfind", f'kMDItemCFBundleIdentifier == "{WECHAT_BUNDLE_ID}"'],
                check=False,
                text=True,
                capture_output=True,
            )
            for line in result.stdout.splitlines():
                p = Path(line.strip())
                if p.name.endswith(".app") and p.exists():
                    return p
        except OSError:
            pass

    for candidate in (Path.home() / "Desktop" / "WeChat.app",):
        if candidate.exists():
            return candidate
    return None


def default_xwechat_base() -> Path:
    return Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"


def locate_user_dirs(base: Path | None = None) -> list[Path]:
    base = base or default_xwechat_base()
    if not base.exists():
        return []
    return sorted([p for p in base.iterdir() if p.is_dir() and p.name.startswith("wxid_")])


def iso_from_mtime(value: float) -> str:
    return dt.datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def user_dir_candidates(base: Path | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for user_dir in locate_user_dirs(base):
        dbs: dict[str, dict[str, Any]] = {}
        mtimes: list[float] = []
        total_key_db_bytes = 0
        for label, rel in WECHAT_KEY_DATABASES.items():
            path = user_dir / rel
            exists = path.exists()
            size = 0
            modified_at = ""
            if exists:
                try:
                    stat = path.stat()
                    size = stat.st_size
                    total_key_db_bytes += size
                    mtimes.append(stat.st_mtime)
                    modified_at = iso_from_mtime(stat.st_mtime)
                except OSError:
                    pass
            dbs[label] = {
                "exists": exists,
                "path": str(path),
                "bytes": size,
                "modified_at": modified_at,
            }

        if not mtimes:
            try:
                mtimes.append(user_dir.stat().st_mtime)
            except OSError:
                pass
        rows.append({
            "name": user_dir.name,
            "path": str(user_dir),
            "latest_modified_at": iso_from_mtime(max(mtimes)) if mtimes else "",
            "total_key_db_bytes": total_key_db_bytes,
            "databases": dbs,
        })
    return sorted(rows, key=lambda item: item["name"])


def format_user_dir_candidate(item: dict[str, Any]) -> str:
    dbs = item.get("databases") if isinstance(item.get("databases"), dict) else {}
    present = [name for name, meta in dbs.items() if isinstance(meta, dict) and meta.get("exists")]
    db_text = ",".join(present) if present else "no key dbs"
    return f"{item.get('name')}  latest={item.get('latest_modified_at') or '-'}  dbs={db_text}  path={item.get('path')}"


def pick_user_dir(base: Path | None = None) -> Path:
    candidates = user_dir_candidates(base)
    if not candidates:
        die(f"No wxid_* user directories found under {base or default_xwechat_base()}")
    if len(candidates) == 1:
        return Path(str(candidates[0]["path"]))
    details = "\n".join("  - " + format_user_dir_candidate(item) for item in candidates)
    die(
        "Multiple WeChat user dirs found; choose one explicitly with --wechat-root or --base:\n"
        + details
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    def add(label: str, ok: bool, detail: str) -> None:
        checks.append((label, ok, detail))

    add("macOS", platform.system() == "Darwin", platform.platform())
    add("Python >= 3.9", sys.version_info >= (3, 9), sys.version.split()[0])
    add("codesign", command_exists("codesign"), shutil.which("codesign") or "missing")
    add("xattr", command_exists("xattr"), shutil.which("xattr") or "missing")

    app = expand_path(args.wechat_app) if args.wechat_app else find_wechat_app()
    add("WeChat.app", bool(app and app.exists()), str(app) if app else "not found")

    base = expand_path(args.base) if args.base else default_xwechat_base()
    user_dirs = user_dir_candidates(base)
    add("WeChat data root", base.exists(), str(base))
    add(
        "WeChat user dirs",
        bool(user_dirs),
        " | ".join(format_user_dir_candidate(item) for item in user_dirs) or "none",
    )

    for module_name, package_name in (
        ("frida", "frida-tools"),
        ("Crypto.Cipher.AES", "pycryptodome"),
        ("zstandard", "zstandard"),
    ):
        try:
            __import__(module_name)
            add(package_name, True, "installed")
        except Exception as exc:
            add(package_name, False, f"{type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(
            [{"check": c[0], "ok": c[1], "detail": c[2]} for c in checks],
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    width = max(len(c[0]) for c in checks)
    for label, ok, detail in checks:
        print(f"{label:<{width}}  {'OK' if ok else 'MISS'}  {detail}")
    return 0


def cmd_locate_user(args: argparse.Namespace) -> int:
    candidates = user_dir_candidates(expand_path(args.base) if args.base else None)
    if not candidates:
        die(f"No wxid_* user directories found under {expand_path(args.base) if args.base else default_xwechat_base()}")
    selected = candidates[0]["path"] if len(candidates) == 1 else ""
    if args.json:
        print(json.dumps({
            "user_dir": selected,
            "selected_user_dir": selected,
            "user_dirs": candidates,
            "warning": "" if selected else "multiple user dirs found; pass --base/--wechat-root explicitly",
        }, ensure_ascii=False, indent=2))
    else:
        if args.print_path and selected:
            print(selected)
        elif args.print_path:
            die("Multiple WeChat user dirs found; run locate-user without --print-path to inspect candidates")
        elif selected:
            print(f"Selected WeChat user dir: {selected}")
        else:
            print("Multiple WeChat user dirs found; choose one explicitly:")
            for item in candidates:
                print("  - " + format_user_dir_candidate(item))
    return 0


def cmd_sign_wechat(args: argparse.Namespace) -> int:
    source = expand_path(args.source) if args.source else find_wechat_app()
    if not source or not source.exists():
        die("Could not find WeChat.app. Pass --source /path/to/WeChat.app")

    dest = expand_path(args.dest)
    if dest.exists():
        if not args.force:
            die(f"Destination already exists: {dest}. Pass --force to replace it.")
        shutil.rmtree(dest)

    info(f"Copying {source} -> {dest}")
    shutil.copytree(source, dest)
    run_checked(["xattr", "-rc", str(dest)])
    run_checked(["codesign", "--force", "--deep", "--sign", "-", str(dest)])

    binary = dest / "Contents/MacOS/WeChat"
    info(f"Signed copy ready: {dest}")
    info(f"Direct launch path: {binary}")
    return 0


def on_frida_message(msg: dict[str, Any], _data: bytes | None) -> None:
    if msg.get("type") == "send":
        print(f"[frida] {msg.get('payload')}", flush=True)
    elif msg.get("type") == "error":
        print(f"[frida-error] {msg}", flush=True)


def wait_for_process(device: Any, name: str, timeout: int) -> int | None:
    start = time.time()
    while time.time() - start < timeout:
        for proc in device.enumerate_processes():
            if proc.name == name:
                return proc.pid
        time.sleep(1)
    return None


def cmd_capture_keys(args: argparse.Namespace) -> int:
    try:
        import frida  # type: ignore
    except Exception as exc:
        die(f"frida is not available: {exc}. Run: python3 -m pip install -r requirements.txt")

    out = expand_path(args.out)
    if out.exists() and not args.append:
        out.unlink()
    ensure_private_file(out)

    wechat_app = expand_path(args.wechat_app) if args.wechat_app else find_wechat_app()
    if not wechat_app or not wechat_app.exists():
        die("Signed WeChat app not found. Run sign-wechat or pass --wechat-app")
    binary = wechat_app / "Contents/MacOS/WeChat"
    if not binary.exists():
        die(f"WeChat binary not found: {binary}")

    device = frida.get_local_device()
    launched_process: subprocess.Popen[Any] | None = None

    if args.mode == "spawn":
        info(f"Spawning {binary}")
        pid = device.spawn([str(binary)])
    else:
        if args.launch:
            info(f"Launching {binary}")
            launched_process = subprocess.Popen([str(binary)])
        else:
            info("Attach mode. Launch the signed WeChat copy directly in another terminal:")
            print(str(binary), flush=True)
        pid = wait_for_process(device, "WeChat", timeout=args.attach_timeout)
        if not pid:
            if launched_process:
                launched_process.terminate()
            die("Timed out waiting for WeChat process")

    info(f"Attaching to PID={pid}")
    session = device.attach(pid)
    script = session.create_script(JS_HOOK.replace("LOG_PATH_PLACEHOLDER", json.dumps(str(out))))
    script.on("message", on_frida_message)
    script.load()
    if args.mode == "spawn":
        device.resume(pid)

    info("Hook active. Open the target chat, File Transfer Assistant, or Favorites inside WeChat.")
    info(f"Waiting {args.wait}s. Key log: {out}")
    start = time.time()
    try:
        while time.time() - start < args.wait:
            time.sleep(5)
            size = out.stat().st_size if out.exists() else 0
            info(f"elapsed={int(time.time() - start)}s log={size}B")
    finally:
        try:
            session.detach()
        except Exception:
            pass
        os.chmod(out, 0o600)

    info(f"Done. Captured keys written to {out}")
    return 0


def parse_keys_log(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for block in re.split(r"\n\s*\n", text):
        rounds = re.search(r"rounds=(\d+)", block)
        salt = re.search(r"salt=([0-9a-fA-F]+)", block)
        dk = re.search(r"dk=([0-9a-fA-F]+)", block)
        if rounds and salt and dk:
            entries.append({
                "rounds": rounds.group(1),
                "salt": salt.group(1).lower(),
                "dk": dk.group(1).lower(),
            })
    return entries


def find_key_for_salt(keys_log: Path, salt_hex: str) -> str:
    for entry in parse_keys_log(keys_log):
        if entry["rounds"] == "256000" and entry["salt"] == salt_hex.lower():
            return entry["dk"]
    return ""


def validate_key_hex(value: str) -> str:
    key = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        die("--key must be exactly 64 lowercase/uppercase hex characters")
    return key


def decrypt_database(enc_db: Path, out_db: Path, key_hex: str) -> tuple[int, str]:
    try:
        from Crypto.Cipher import AES  # type: ignore
    except Exception as exc:
        die(f"pycryptodome is not available: {exc}. Run: python3 -m pip install -r requirements.txt")

    size = enc_db.stat().st_size
    if size < PAGE_SIZE or size % PAGE_SIZE != 0:
        die(f"Unexpected DB size {size}; expected a positive multiple of {PAGE_SIZE}")

    key = bytes.fromhex(key_hex)
    total_pages = size // PAGE_SIZE
    with enc_db.open("rb") as src:
        salt_hex = src.read(16).hex()
    out_db.parent.mkdir(parents=True, exist_ok=True)

    with enc_db.open("rb") as src, out_db.open("wb") as dst:
        for index in range(total_pages):
            page = src.read(PAGE_SIZE)
            offset = 16 if index == 0 else 0
            encrypted = page[offset:PAGE_SIZE - RESERVE]
            iv = page[PAGE_SIZE - RESERVE: PAGE_SIZE - RESERVE + IV_LEN]
            plain = AES.new(key, AES.MODE_CBC, iv).decrypt(encrypted)
            header_len = len(SQLITE_HEADER) if index == 0 else 0
            if index == 0:
                dst.write(SQLITE_HEADER)
            dst.write(plain)
            pad = PAGE_SIZE - header_len - len(plain)
            if pad > 0:
                dst.write(b"\x00" * pad)

    return total_pages, salt_hex


def verify_sqlite(path: Path) -> str:
    try:
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute("PRAGMA schema_version")
        cur.fetchone()
        cur.execute("PRAGMA integrity_check")
        result = to_text(cur.fetchone()[0])
        con.close()
        return result
    except Exception as exc:
        raise RuntimeError(f"SQLite verification failed: {exc}") from exc


def cmd_decrypt(args: argparse.Namespace) -> int:
    enc_db = expand_path(args.db)
    out_db = expand_path(args.out)
    if not enc_db.exists():
        die(f"Encrypted DB not found: {enc_db}")

    with enc_db.open("rb") as src:
        salt_hex = src.read(16).hex()
    key = validate_key_hex(args.key) if args.key else ""
    if not key:
        keys_log = expand_path(args.keys_log)
        if not keys_log.exists():
            die(f"No --key provided and keys log not found: {keys_log}")
        info(f"Looking up key for salt={salt_hex} in {keys_log}")
        key = find_key_for_salt(keys_log, salt_hex)
        if not key:
            die("No matching key found. Re-run capture-keys and open the target data in WeChat.")
    total_pages, salt_hex = decrypt_database(enc_db, out_db, key)
    info(f"Decrypted pages={total_pages} salt={salt_hex}")
    info(f"Wrote {out_db} size={out_db.stat().st_size}")

    if not args.no_verify:
        result = verify_sqlite(out_db)
        info(f"SQLite integrity_check={result}")
    return 0


def msg_table_for_target(target: str) -> str:
    return "Msg_" + hashlib.md5(target.encode("utf-8")).hexdigest()


def validate_msg_table_name(table: str) -> str:
    if not re.fullmatch(r"Msg_[0-9a-f]{32}", table):
        die(f"Unsafe message table name: {table}")
    return table


def connect_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        die(f"SQLite DB not found: {db_path}")
    con = sqlite3.connect(db_path)
    con.text_factory = bytes
    return con


def get_tables(con: sqlite3.Connection) -> list[str]:
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
    return sorted(to_text(row[0]) for row in cur.fetchall())


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def count_table_rows(con: sqlite3.Connection, table: str) -> int:
    table = validate_msg_table_name(table)
    try:
        cur = con.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])
    except sqlite3.DatabaseError:
        return -1


def name2id_targets(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not table_exists(con, "Name2Id"):
        return {}
    cur = con.cursor()
    columns = [to_text(row[1]) for row in cur.execute("PRAGMA table_info(Name2Id)").fetchall()]
    if "user_name" not in columns:
        return {}

    select_cols = ["rowid", "user_name"]
    if "is_session" in columns:
        select_cols.append("is_session")
    cur.execute(f"SELECT {', '.join(select_cols)} FROM Name2Id")

    targets: dict[str, dict[str, Any]] = {}
    for row in cur.fetchall():
        user_name = to_text(row[1])
        if not user_name:
            continue
        item = {"rowid": int(row[0]), "target": user_name}
        if len(row) > 2:
            item["is_session"] = int(row[2]) if row[2] is not None else None
        targets[user_name] = item
    return targets


def cmd_list_targets(args: argparse.Namespace) -> int:
    con = connect_db(expand_path(args.db))
    tables = set(get_tables(con))
    by_target = name2id_targets(con)

    rows: list[dict[str, Any]] = []
    matched_tables: set[str] = set()
    for target, meta in by_target.items():
        table = msg_table_for_target(target)
        if table in tables:
            matched_tables.add(table)
            rows.append({
                "target": target,
                "table": table,
                "messages": count_table_rows(con, table),
                "is_session": meta.get("is_session"),
            })

    for table in sorted(tables - matched_tables):
        rows.append({
            "target": "",
            "table": table,
            "messages": count_table_rows(con, table),
            "is_session": None,
        })

    rows.sort(key=lambda r: r["messages"], reverse=True)
    if args.limit:
        rows = rows[:args.limit]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(f"{'messages':>8}  {'target':<42}  table")
        print("-" * 92)
        for row in rows:
            target = row["target"] or "(unmapped)"
            if len(target) > 42:
                target = target[:39] + "..."
            print(f"{row['messages']:>8}  {target:<42}  {row['table']}")
    con.close()
    return 0


def decode_content(raw: bytes | str | None) -> bytes:
    if raw is None:
        return b""
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")
    data = bytes(raw)
    if not data:
        return b""
    if data[:4] == b"\x28\xB5\x2F\xFD":
        try:
            import zstandard as zstd  # type: ignore
            return zstd.ZstdDecompressor().decompress(data, max_output_size=100 * 1024 * 1024)
        except Exception:
            return data
    return data


def xml_field(text: str, tag: str) -> str:
    match = re.search(rf"<{re.escape(tag)}[^>]*>(.*?)</{re.escape(tag)}>", text, re.DOTALL)
    return html.unescape(match.group(1).strip()) if match else ""


def xml_attr(text: str, tag: str, attr: str) -> str:
    match = re.search(rf'<{re.escape(tag)}[^>]*\b{re.escape(attr)}="([^"]+)"', text, re.DOTALL)
    return html.unescape(match.group(1).strip()) if match else ""


def first_url(text: str) -> str:
    match = re.search(r"https?://[^\s\"'<>]+", text)
    return match.group(0) if match else ""


def render_plain_text(text: str) -> str:
    cleaned = text.replace("\x00", "").replace("\r", "")
    match = re.search(
        r"([\x20-\x7e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef][^\x00-\x08\x0b-\x1f]*)",
        cleaned,
        re.DOTALL,
    )
    return (match.group(1) if match else cleaned).strip()


def format_size(num_bytes: str) -> str:
    try:
        size = float(num_bytes)
    except ValueError:
        return ""
    units = ["B", "KB", "MB", "GB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx else f"{int(size)} {units[idx]}"


def format_message(local_type: int, raw: bytes | str | None) -> str:
    decoded = decode_content(raw)
    text = decoded.decode("utf-8", errors="replace") if decoded else ""

    if local_type == 1:
        return render_plain_text(text)
    if local_type == 3:
        md5 = xml_attr(text, "img", "md5")
        return f"[图片] md5={md5}" if md5 else "[图片]"
    if local_type == 34:
        length = xml_attr(text, "voicemsg", "voicelength")
        return f"[语音 {length}ms]" if length else "[语音]"
    if local_type == 43:
        length = xml_attr(text, "videomsg", "playlength")
        return f"[视频 {length}s]" if length else "[视频]"
    if local_type == 47:
        return "[表情]"
    if local_type == 48:
        label = xml_attr(text, "location", "poiname") or xml_attr(text, "location", "label")
        x = xml_attr(text, "location", "x")
        y = xml_attr(text, "location", "y")
        coords = f" ({x}, {y})" if x and y else ""
        return f"[位置] {label}{coords}".strip()
    if local_type == 49 or local_type > 100:
        title = xml_field(text, "title")
        desc = xml_field(text, "des") or xml_field(text, "desc")
        url = xml_field(text, "url") or first_url(text)
        source = xml_field(text, "sourcedisplayname")
        filename = xml_field(text, "filename")
        total_len = xml_field(text, "totallen")

        if filename:
            size = format_size(total_len)
            return f"[文件] {filename}" + (f" ({size})" if size else "")
        if title and url:
            lines = [f"**[{title}]({url})**"]
            if source:
                lines.append(f"_Source: {source}_")
            if desc:
                lines.append(f"> {desc}")
            return "\n".join(lines)
        if title:
            return f"**{title}**" + (f"\n> {desc}" if desc else "")
        if url:
            return url
        rendered = render_plain_text(text)
        return rendered or f"[类型{local_type}]"

    url = first_url(text)
    if url:
        return url
    rendered = render_plain_text(text)
    return rendered or f"[类型{local_type}]"


def message_type_label(local_type: Any, fallback: str = "") -> str:
    try:
        return TYPE_MAP.get(int(local_type), f"type{int(local_type)}")
    except (TypeError, ValueError):
        return fallback or "message"


def parse_epoch(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        raw = int(text)
        return raw // 1000 if raw > 10_000_000_000 else raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(dt.datetime.strptime(text[:19], fmt).timestamp())
        except ValueError:
            continue
    return None


def parse_weflow_title(data: dict[str, Any], source_name: str = "WeFlow") -> str:
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    for value in (
        session.get("displayName"),
        session.get("nickname"),
        session.get("remark"),
        session.get("username"),
        meta.get("name"),
        meta.get("groupId"),
        data.get("talker"),
        source_name,
    ):
        if value:
            return str(value)
    return source_name


def chatlab_member_map(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    members = data.get("members")
    if not isinstance(members, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        key = member.get("platformId") or member.get("id") or member.get("accountName")
        if key:
            result[str(key)] = member
    return result


def markdown_link(title: str, url: str) -> str:
    return f"[{title}]({url})" if title and url else url or title


def normalize_weflow_message(message: dict[str, Any], members: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    ts = (
        parse_epoch(message.get("createTime"))
        or parse_epoch(message.get("timestamp"))
        or parse_epoch(message.get("formattedTime"))
    )
    if ts is None:
        return None

    sender_id = message.get("senderUsername") or message.get("sender") or message.get("senderId") or ""
    member = members.get(str(sender_id), {}) if sender_id else {}
    sender = (
        message.get("senderDisplayName")
        or message.get("groupNickname")
        or member.get("groupNickname")
        or message.get("accountName")
        or member.get("accountName")
        or sender_id
        or ("me" if message.get("isSend") in (1, True, "1", "true") else "")
        or "unknown"
    )

    local_type = message.get("localType")
    type_label = str(message.get("type") or message_type_label(local_type))
    content = (
        message.get("parsedContent")
        or message.get("content")
        or message.get("rawContent")
        or ""
    )

    link_title = message.get("linkTitle") or message.get("title")
    link_url = message.get("linkUrl") or message.get("url")
    if link_title or link_url:
        content = markdown_link(str(link_title or link_url), html.unescape(str(link_url or "")))

    media_path = (
        message.get("mediaLocalPath")
        or message.get("mediaPath")
        or message.get("localPath")
        or ""
    )
    media_url = message.get("mediaUrl") or ""
    media_type = message.get("mediaType") or ""

    quoted_sender = message.get("quotedSender") or ""
    quoted_content = message.get("quotedContent") or ""
    if quoted_sender or quoted_content:
        quote_lines = [f"> {quoted_sender or 'quoted'}"]
        for line in str(quoted_content).splitlines():
            quote_lines.append(f"> {line}")
        content = (str(content).strip() + "\n\n" if str(content).strip() else "") + "\n".join(quote_lines)

    return {
        "create_time": ts,
        "local_id": str(message.get("localId") or message.get("platformMessageId") or message.get("serverId") or ""),
        "type_label": type_label,
        "local_type": local_type,
        "sender": str(sender),
        "content": normalize_text_for_markdown(content),
        "media_path": str(media_path) if media_path else "",
        "media_url": str(media_url) if media_url else "",
        "media_type": str(media_type) if media_type else "",
    }


def normalize_text_for_markdown(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return json.dumps(value, ensure_ascii=False, indent=2)


def normalize_weflow_payload(data: dict[str, Any], source_name: str = "WeFlow") -> tuple[str, list[dict[str, Any]]]:
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        die("WeFlow JSON does not contain a messages[] array")
    members = chatlab_member_map(data)
    title = parse_weflow_title(data, source_name)
    messages = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        normalized = normalize_weflow_message(item, members)
        if normalized:
            messages.append(normalized)
    messages.sort(key=lambda item: (item["create_time"], item.get("local_id") or ""))
    return title, messages


def load_json_maybe_wrapped(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [idx for idx in (text.find("{"), text.find("[")) if idx >= 0]
        ends = [idx for idx in (text.rfind("}"), text.rfind("]")) if idx >= 0]
        if not starts or not ends:
            raise
        return json.loads(text[min(starts): max(ends) + 1])


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def epoch_to_iso(value: int | None) -> str:
    if value is None:
        return ""
    return dt.datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def extract_wx_sessions(data: Any) -> list[dict[str, Any]]:
    sessions = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(sessions, list):
        return []
    return [item for item in sessions if isinstance(item, dict)]


def is_placeholder_wx_session(item: dict[str, Any]) -> bool:
    chat_type = str(item.get("chat_type") or item.get("type") or "").lower()
    username = str(item.get("username") or item.get("id") or item.get("chat") or "")
    name = str(item.get("chat") or item.get("name") or item.get("display_name") or "")
    return (
        chat_type in {"folded", "placeholder"}
        or username.startswith("@placeholder")
        or name.startswith("@placeholder")
    )


def normalize_wx_session(item: dict[str, Any]) -> dict[str, Any]:
    username = str(first_present(item, "username", "user_name", "id", "wxid", "chat") or "")
    display_name = str(
        first_present(item, "chat", "name", "display_name", "displayName", "remark", "nickname")
        or username
    )
    chat_type = str(first_present(item, "chat_type", "type") or "")
    is_group = (
        bool(item.get("is_group"))
        or bool(item.get("isChatRoom"))
        or username.endswith("@chatroom")
        or chat_type == "group"
    )
    if not chat_type:
        chat_type = "group" if is_group else "single"
    aliases = {
        value for value in (
            username,
            str(item.get("id") or ""),
            str(item.get("chat") or ""),
            str(item.get("name") or ""),
            str(item.get("display_name") or ""),
            str(item.get("displayName") or ""),
            str(item.get("talker") or ""),
            str(item.get("talkerName") or ""),
        )
        if value
    }
    return {
        "username": username,
        "display_name": display_name,
        "chat_type": chat_type,
        "is_group": is_group,
        "unread": item.get("unread", ""),
        "last_message_at": epoch_to_iso(parse_epoch(
            item.get("timestamp") or item.get("time") or item.get("lastTime") or item.get("last_message_at")
        )),
        "aliases": sorted(aliases),
        "matched": True,
    }


def minimal_wx_session(chat_id: str, *, matched: bool = False) -> dict[str, Any]:
    is_group = chat_id.endswith("@chatroom")
    return {
        "username": chat_id,
        "display_name": chat_id,
        "chat_type": "group" if is_group else "single",
        "is_group": is_group,
        "unread": "",
        "last_message_at": "",
        "aliases": [chat_id],
        "matched": matched,
    }


def format_wx_session_candidate(item: dict[str, Any]) -> str:
    return (
        f"{item.get('username') or '-'}"
        f"  name={item.get('display_name') or '-'}"
        f"  type={item.get('chat_type') or '-'}"
        f"  unread={item.get('unread') if item.get('unread') != '' else '-'}"
    )


def die_wx_session_candidates(reason: str, matches: list[dict[str, Any]]) -> None:
    details = "\n".join("  - " + format_wx_session_candidate(item) for item in matches[:20])
    if len(matches) > 20:
        details += f"\n  ... and {len(matches) - 20} more"
    die(reason + ("\nCandidates:\n" + details if details else ""))


def resolve_wx_session(
    sessions: list[dict[str, Any]],
    *,
    chat_id: str | None = None,
    chat_name: str | None = None,
) -> dict[str, Any]:
    normalized = [
        normalize_wx_session(item)
        for item in sessions
        if not is_placeholder_wx_session(item)
    ]
    if chat_id:
        exact = [item for item in normalized if chat_id in set(item.get("aliases", []))]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            die_wx_session_candidates(f"Multiple sessions matched chat id {chat_id!r}; use a unique username.", exact)
        if chat_id.startswith("@placeholder"):
            die(f"Refusing to import placeholder/folded wx-cli session: {chat_id}")
        return minimal_wx_session(chat_id)

    if not chat_name:
        die("A chat id or chat name is required")

    exact = [
        item for item in normalized
        if chat_name == item.get("display_name") or chat_name == item.get("username")
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        die_wx_session_candidates(
            f"Multiple sessions matched chat name {chat_name!r}; rerun wx-sessions and pass --chat-id.",
            exact,
        )

    needle = chat_name.lower()
    fuzzy = [
        item for item in normalized
        if needle in str(item.get("display_name") or "").lower()
        or needle in str(item.get("username") or "").lower()
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        die_wx_session_candidates(
            f"Multiple fuzzy sessions matched {chat_name!r}; rerun wx-sessions and pass --chat-id.",
            fuzzy,
        )
    die(f"No wx-cli session matched {chat_name!r}. Run wx-sessions with a larger --limit or pass --chat-id.")


def looks_like_chat_id(value: str) -> bool:
    return (
        value == "filehelper"
        or value.endswith("@chatroom")
        or value.startswith("wxid_")
        or value.startswith("gh_")
    )


def wx_cli_message_array(data: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(data, list):
        return "wx-cli", [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        die("wx-cli JSON root must be an object or array")

    title = str(
        data.get("chat")
        or data.get("name")
        or data.get("display_name")
        or data.get("title")
        or "wx-cli"
    )
    for key_path in (
        ("messages",),
        ("data", "messages"),
        ("result", "messages"),
        ("history", "messages"),
    ):
        cursor: Any = data
        for key in key_path:
            cursor = cursor.get(key) if isinstance(cursor, dict) else None
        if isinstance(cursor, list):
            return title, [item for item in cursor if isinstance(item, dict)]
    die("wx-cli JSON does not contain messages[]")


WX_CLI_RECOGNIZED_MESSAGE_KEYS = {
    "timestamp", "create_time", "created_at", "createTime", "time_ts", "time", "datetime",
    "sender", "sender_name", "senderDisplayName", "senderName", "groupNickname", "from", "from_name", "talker",
    "talkerName", "isChatRoom", "isSelf", "subType", "contents",
    "content", "text", "message", "parsedContent", "rawContent",
    "type", "msg_type", "message_type", "local_type", "localType",
    "media_path", "mediaPath", "media_url", "mediaUrl", "media_type", "mediaType",
    "local_path", "localPath", "file_path", "filePath", "path",
    "url", "linkUrl", "linkTitle", "title", "filename", "fileName",
    "local_id", "localId", "id", "seq", "msg_id", "msgId", "platformMessageId", "serverId",
}


def wx_message_dedupe_key(message: dict[str, Any]) -> str:
    local_id = str(message.get("local_id") or "")
    if local_id:
        return "local_id:" + local_id
    digest = hashlib.sha1(
        "\n".join([
            str(message.get("create_time") or ""),
            str(message.get("sender") or ""),
            str(message.get("type_label") or ""),
            str(message.get("content") or ""),
            str(message.get("media_path") or ""),
            str(message.get("media_url") or ""),
        ]).encode("utf-8", errors="replace")
    ).hexdigest()
    return "fingerprint:" + digest


def normalize_wx_cli_payload_with_audit(
    data: Any,
    source_name: str = "wx-cli",
    since: str | None = None,
    until: str | None = None,
    initial_audit: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    title, raw_messages = wx_cli_message_array(data)
    if title == "wx-cli":
        title = source_name

    audit = {
        "pages_fetched": 1,
        "raw_message_count": len(raw_messages),
        "normalized_count": 0,
        "deduped_count": 0,
        "filtered_count": 0,
        "dropped_count": 0,
        "first_message_at": "",
        "last_message_at": "",
        "warnings": [],
        "raw_debug": {
            "unknown_message_keys": [],
            "date_filtered_out": 0,
        },
    }
    if initial_audit:
        for key, value in initial_audit.items():
            if key == "warnings":
                audit["warnings"].extend(value)
            elif key == "raw_debug" and isinstance(value, dict):
                audit["raw_debug"].update(value)
            else:
                audit[key] = value
        audit["raw_message_count"] = len(raw_messages)

    unknown_keys: set[str] = set(audit["raw_debug"].get("unknown_message_keys", []))
    messages: list[dict[str, Any]] = []
    for item in raw_messages:
        unknown_keys.update(set(item.keys()) - WX_CLI_RECOGNIZED_MESSAGE_KEYS)
        ts = (
            parse_epoch(first_present(item, "timestamp", "create_time", "created_at", "createTime", "time_ts"))
            or parse_epoch(first_present(item, "time", "datetime"))
        )
        if ts is None:
            audit["dropped_count"] += 1
            continue

        sender = (
            first_present(item, "sender", "sender_name", "senderDisplayName", "senderName", "groupNickname")
            or first_present(item, "from", "from_name", "talker")
            or "unknown"
        )
        content = first_present(item, "content", "text", "message", "parsedContent", "rawContent")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        if content is None:
            link_title = first_present(item, "linkTitle", "title")
            link_url = first_present(item, "linkUrl", "url")
            filename = first_present(item, "filename", "fileName")
            if link_title or link_url:
                content = markdown_link(str(link_title or link_url), html.unescape(str(link_url or "")))
            elif filename:
                content = f"[文件] {filename}"
            else:
                content = ""

        local_type = first_present(item, "local_type", "localType")
        type_value = first_present(item, "type", "msg_type", "message_type")
        type_label = str(type_value or message_type_label(local_type))
        media_path = first_present(
            item,
            "media_path",
            "mediaPath",
            "local_path",
            "localPath",
            "file_path",
            "filePath",
            "path",
        )
        media_url = first_present(item, "media_url", "mediaUrl", "url", "linkUrl")
        media_type = first_present(item, "media_type", "mediaType") or type_label
        local_id = first_present(item, "local_id", "localId", "id", "seq", "msg_id", "msgId", "platformMessageId", "serverId")

        messages.append({
            "create_time": ts,
            "local_id": str(local_id or ""),
            "type_label": type_label,
            "local_type": local_type,
            "sender": str(sender),
            "content": normalize_text_for_markdown(content),
            "media_path": str(media_path) if media_path else "",
            "media_url": str(media_url) if media_url else "",
            "media_type": str(media_type) if media_type else "",
        })

    messages.sort(key=lambda item: (item["create_time"], item.get("local_id") or ""))
    audit["normalized_count"] = len(messages)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for message in messages:
        key = wx_message_dedupe_key(message)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        deduped.append(message)
    if duplicates:
        audit["warnings"].append(f"Dropped {duplicates} duplicate wx-cli messages during import.")
    audit["deduped_count"] = len(deduped)

    since_ts = parse_date(since) if since else None
    until_ts = parse_date(until, exclusive_end=True) if until else None
    if since_ts and until_ts and since_ts >= until_ts:
        die("--since must be earlier than --until")
    filtered = [
        item for item in deduped
        if (since_ts is None or item["create_time"] >= since_ts)
        and (until_ts is None or item["create_time"] < until_ts)
    ]
    audit["filtered_count"] = len(filtered)
    audit["raw_debug"]["date_filtered_out"] = len(deduped) - len(filtered)
    audit["raw_debug"]["unknown_message_keys"] = sorted(unknown_keys)
    if unknown_keys:
        audit["warnings"].append("Unknown wx-cli message keys observed: " + ", ".join(sorted(unknown_keys)))
    if audit["dropped_count"]:
        audit["warnings"].append(f"Dropped {audit['dropped_count']} wx-cli messages without a parseable timestamp.")
    if filtered:
        audit["first_message_at"] = epoch_to_iso(filtered[0]["create_time"])
        audit["last_message_at"] = epoch_to_iso(filtered[-1]["create_time"])

    return title, filtered, audit


def normalize_wx_cli_payload(data: Any, source_name: str = "wx-cli") -> tuple[str, list[dict[str, Any]]]:
    title, messages, _audit = normalize_wx_cli_payload_with_audit(data, source_name)
    return title, messages


def media_markdown(path_or_url: str, label: str = "media") -> str:
    lower = path_or_url.lower()
    escaped = path_or_url.replace(" ", "%20")
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic")):
        return f"![{label}]({escaped})"
    return f"[{label}]({escaped})"


def copy_weflow_media(media_path: str, source_json: Path | None, month_dir: Path, used_names: set[str]) -> str:
    if not media_path:
        return ""
    source = expand_path(media_path)
    if not source.is_absolute() and source_json:
        source = (source_json.parent / source).resolve()
    if not source.exists() or not source.is_file():
        return media_path

    attachments = month_dir / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    base = safe_segment(source.stem) + source.suffix.lower()
    candidate = base
    index = 2
    while candidate in used_names or (attachments / candidate).exists():
        candidate = f"{safe_segment(source.stem)}-{index}{source.suffix.lower()}"
        index += 1
    used_names.add(candidate)
    dest = attachments / candidate
    shutil.copy2(source, dest)
    return str(Path("attachments") / candidate)


def write_weflow_day_file(
    path: Path,
    title: str,
    day: str,
    messages: list[dict[str, Any]],
    mode: str,
    source_label: str = "weflow",
) -> bool:
    if mode == "skip" and path.exists():
        return False
    exported_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "---",
        f"source: {source_label}",
        f"title: {yaml_string(title)}",
        f"date: {yaml_string(day)}",
        f"message_count: {len(messages)}",
        f"exported_at: {yaml_string(exported_at)}",
        "---",
        "",
        f"# {day} · {title}",
        "",
    ]
    for msg in messages:
        when = dt.datetime.fromtimestamp(msg["create_time"]).strftime("%H:%M:%S")
        heading_bits = [when, msg.get("sender") or "unknown", msg.get("type_label") or "message"]
        lines.append("## " + " · ".join(heading_bits))
        lines.append("")
        if msg.get("local_id"):
            lines.append(f"`local_id`: {msg['local_id']}")
            lines.append("")
        body = msg.get("content") or ""
        media_ref = msg.get("media_ref") or msg.get("media_url") or ""
        if media_ref:
            lines.append(media_markdown(media_ref, msg.get("media_type") or msg.get("type_label") or "media"))
            lines.append("")
        lines.append(body if body else "_(empty)_")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True


def export_weflow_messages(
    messages: list[dict[str, Any]],
    title: str,
    vault: Path,
    folder: str,
    subfolder: str | None,
    mode: str,
    since: str | None,
    until: str | None,
    source_json: Path | None = None,
    copy_media: bool = True,
    source_label: str = "weflow",
    manifest_name: str = "_weflow_import_manifest.json",
    manifest_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    since_ts = parse_date(since) if since else None
    until_ts = parse_date(until, exclusive_end=True) if until else None
    if since_ts and until_ts and since_ts >= until_ts:
        die("--since must be earlier than --until")

    filtered = [
        item for item in messages
        if (since_ts is None or item["create_time"] >= since_ts)
        and (until_ts is None or item["create_time"] < until_ts)
    ]
    out_root = safe_vault_path(vault, folder, subfolder or safe_segment(title))
    out_root.mkdir(parents=True, exist_ok=True)

    by_day: dict[str, list[dict[str, Any]]] = {}
    for item in filtered:
        day = dt.datetime.fromtimestamp(item["create_time"]).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(dict(item))

    used_media_names: set[str] = set()
    copied_media = 0
    for day, day_messages in by_day.items():
        month_dir = out_root / day[:7]
        if copy_media:
            for msg in day_messages:
                media_path = msg.get("media_path") or ""
                if media_path:
                    ref = copy_weflow_media(media_path, source_json, month_dir, used_media_names)
                    if ref and ref != media_path:
                        copied_media += 1
                    msg["media_ref"] = ref

    written = 0
    skipped = 0
    for day in sorted(by_day):
        if write_weflow_day_file(out_root / day[:7] / f"{day}.md", title, day, by_day[day], mode, source_label):
            written += 1
        else:
            skipped += 1

    manifest = {
        "source": source_label,
        "title": title,
        "output": str(out_root),
        "source_json": str(source_json) if source_json else "",
        "exported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "message_count": len(filtered),
        "day_files_written": written,
        "day_files_skipped": skipped,
        "media_copied": copied_media,
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    (out_root / manifest_name).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def cmd_import_weflow_json(args: argparse.Namespace) -> int:
    input_path = expand_path(args.input)
    if not input_path.exists():
        die(f"WeFlow JSON not found: {input_path}")
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        die("WeFlow JSON root must be an object")
    title, messages = normalize_weflow_payload(data, input_path.stem)
    manifest = export_weflow_messages(
        messages,
        args.title or title,
        expand_path(args.vault),
        args.folder,
        args.subfolder,
        args.mode,
        args.since,
        args.until,
        source_json=input_path,
        copy_media=not args.no_media_copy,
    )
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        info(f"Imported {manifest['message_count']} WeFlow messages")
        info(f"day_files_written={manifest['day_files_written']} skipped={manifest['day_files_skipped']}")
        info(f"output={manifest['output']}")
    return 0


def load_weflow_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or DEFAULT_WEFLOW_CONFIG
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def weflow_base_url_from_config(config: dict[str, Any]) -> str:
    host = str(config.get("httpApiHost") or "127.0.0.1").strip() or "127.0.0.1"
    port = config.get("httpApiPort") or 5031
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        port_num = 5031
    return f"http://{host}:{port_num}"


def resolve_weflow_api_options(args: argparse.Namespace) -> tuple[str, str]:
    config = load_weflow_config(expand_path(args.config) if getattr(args, "config", None) else None)
    base_url = (
        getattr(args, "base_url", None)
        or os.environ.get("WEFLOW_BASE_URL")
        or weflow_base_url_from_config(config)
    )
    token = (
        getattr(args, "token", None)
        or os.environ.get("WEFLOW_TOKEN")
        or str(config.get("httpApiToken") or "")
    )
    return base_url.rstrip("/"), token


def weflow_api_get(base_url: str, endpoint: str, params: dict[str, Any], token: str) -> dict[str, Any]:
    clean_base = base_url.rstrip("/")
    query = {k: v for k, v in params.items() if v not in (None, "", False)}
    if token:
        query["access_token"] = token
    url = f"{clean_base}{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        die(f"WeFlow API HTTP {exc.code}: {body[:500]}")
    except urllib.error.URLError as exc:
        die(f"Cannot reach WeFlow API at {clean_base}: {exc}")


def compact_date(value: str | None) -> str | None:
    return value.replace("-", "") if value else None


def fetch_weflow_messages_api(args: argparse.Namespace, base_url: str, token: str) -> tuple[str, list[dict[str, Any]]]:
    all_messages: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = weflow_api_get(
            base_url,
            "/api/v1/messages",
            {
                "talker": args.talker,
                "limit": args.limit,
                "offset": offset,
                "start": compact_date(args.since),
                "end": compact_date(args.until),
                "media": "1" if args.media else "",
                "image": "1" if args.media else "",
                "voice": "1" if args.media else "",
                "video": "1" if args.media else "",
                "emoji": "1" if args.media else "",
                "format": "json",
            },
            token,
        )
        messages = payload.get("messages")
        if not isinstance(messages, list):
            die("WeFlow API response does not contain messages[]")
        title, normalized = normalize_weflow_payload(
            {"talker": payload.get("talker") or args.talker, "messages": messages},
            args.talker,
        )
        all_messages.extend(normalized)
        if not payload.get("hasMore") or not messages:
            break
        offset += len(messages)
    return args.talker, all_messages


def fetch_weflow_chatlab_api(args: argparse.Namespace, base_url: str, token: str) -> tuple[str, list[dict[str, Any]]]:
    all_messages: list[dict[str, Any]] = []
    title = args.talker
    since_ts = parse_date(args.since) if args.since else None
    end_ts = parse_date(args.until, exclusive_end=True) if args.until else None
    offset = 0
    while True:
        endpoint = "/api/v1/sessions/" + urllib.parse.quote(args.talker, safe="") + "/messages"
        payload = weflow_api_get(
            base_url,
            endpoint,
            {
                "since": since_ts,
                "end": end_ts,
                "limit": min(args.limit, 5000),
                "offset": offset,
            },
            token,
        )
        page_title, normalized = normalize_weflow_payload(payload, args.talker)
        title = page_title or title
        all_messages.extend(normalized)
        sync = payload.get("sync") if isinstance(payload.get("sync"), dict) else {}
        if not sync.get("hasMore") or not normalized:
            break
        next_offset = sync.get("nextOffset")
        try:
            offset = int(next_offset)
        except (TypeError, ValueError):
            offset += len(normalized)
    return title, all_messages


def cmd_import_weflow_api(args: argparse.Namespace) -> int:
    base_url, token = resolve_weflow_api_options(args)
    if args.api_mode == "chatlab" and not args.media:
        title, all_messages = fetch_weflow_chatlab_api(args, base_url, token)
    else:
        title, all_messages = fetch_weflow_messages_api(args, base_url, token)
    all_messages.sort(key=lambda item: (item["create_time"], item.get("local_id") or ""))
    manifest = export_weflow_messages(
        all_messages,
        args.title or args.subfolder or title or args.talker,
        expand_path(args.vault),
        args.folder,
        args.subfolder,
        args.mode,
        None,
        None,
        source_json=None,
        copy_media=not args.no_media_copy,
    )
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        info(f"Imported {manifest['message_count']} messages from WeFlow API")
        info(f"day_files_written={manifest['day_files_written']} skipped={manifest['day_files_skipped']}")
        info(f"output={manifest['output']}")
    return 0


def run_wx_cli_json(
    args: argparse.Namespace,
    command: str,
    *,
    chat: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    cli_resolved: tuple[str, str] | None = None,
    save_raw: bool = True,
) -> Any:
    kind, executable = cli_resolved or resolve_wx_cli(getattr(args, "cli", "auto"), getattr(args, "binary", None))
    effective_limit = limit if limit is not None else getattr(args, "limit", 100)
    if command == "sessions":
        if kind == "wx":
            cmd = [executable, "sessions", "-n", str(effective_limit), "--json"]
        else:
            cmd = [executable, "sessions", "--limit", str(effective_limit), "--format", "json"]
    else:
        history_chat = chat or getattr(args, "chat", None)
        if not history_chat:
            die("A chat id/name is required for wx-cli history")
        if kind == "wx":
            cmd = [executable, "history", history_chat, "-n", str(effective_limit)]
            if offset is not None:
                cmd.extend(["--offset", str(offset)])
            cmd.append("--json")
            if args.since:
                cmd.extend(["--since", args.since])
            if args.until:
                cmd.extend(["--until", args.until])
            # jackwener/wx-cli 0.1.x 不支持 --media; 媒体路径由 message 字段直出
            # if getattr(args, "media", False):
            #     cmd.append("--media")
        else:
            cmd = [executable, "history", history_chat, "--limit", str(effective_limit), "--format", "json"]
            if offset is not None:
                cmd.extend(["--offset", str(offset)])
            if args.since:
                cmd.extend(["--start-time", args.since])
            if args.until:
                cmd.extend(["--end-time", args.until])
            if getattr(args, "media", False):
                cmd.append("--media")

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        die(
            "wx-cli command failed:\n"
            + " ".join(cmd)
            + "\n\nSTDOUT:\n"
            + result.stdout
            + "\nSTDERR:\n"
            + result.stderr
        )
    if save_raw and getattr(args, "raw_output", None):
        raw_path = expand_path(args.raw_output)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(result.stdout, encoding="utf-8")
    return load_json_maybe_wrapped(result.stdout)


def resolve_import_wx_session(args: argparse.Namespace) -> tuple[str, dict[str, Any], list[str]]:
    chat_id = getattr(args, "chat_id", None) or None
    chat_name = getattr(args, "chat_name", None) or None
    legacy_chat = getattr(args, "chat", None) or None
    if not chat_id and not chat_name and legacy_chat:
        if looks_like_chat_id(legacy_chat):
            chat_id = legacy_chat
        else:
            chat_name = legacy_chat
    if not chat_id and not chat_name:
        die("--chat-id, --chat-name, or --chat is required unless --input-json is provided")

    raw_ref = chat_id or chat_name or legacy_chat or ""
    warnings: list[str] = []
    if not getattr(args, "resolve_chat", True):
        warnings.append("Chat resolution disabled; importing with the raw chat reference.")
        return raw_ref, minimal_wx_session(raw_ref), warnings

    sessions_data = run_wx_cli_json(args, "sessions", limit=getattr(args, "session_limit", 500), save_raw=False)
    sessions = extract_wx_sessions(sessions_data)
    if not sessions:
        if chat_id:
            warnings.append("wx-sessions returned no usable sessions; importing the exact --chat-id without metadata.")
            return chat_id, minimal_wx_session(chat_id), warnings
        die("wx-sessions returned no usable sessions; pass --chat-id with --no-resolve-chat to import a known id.")

    resolved = resolve_wx_session(sessions, chat_id=chat_id, chat_name=chat_name)
    if chat_id and not resolved.get("matched"):
        warnings.append("Exact --chat-id was not present in recent wx-sessions output; metadata is incomplete.")
    return str(resolved["username"]), resolved, warnings


def fetch_wx_cli_history(args: argparse.Namespace, chat_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cli_resolved = resolve_wx_cli(getattr(args, "cli", "auto"), getattr(args, "binary", None))
    kind, _executable = cli_resolved
    max_messages = int(getattr(args, "max_messages", None) or getattr(args, "limit", 5000))
    page_size = int(getattr(args, "page_size", None) or min(max_messages, 500))
    if max_messages <= 0:
        die("--max-messages/--limit must be greater than 0")
    if page_size <= 0:
        die("--page-size must be greater than 0")

    raw_messages: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    offset = 0
    pages_fetched = 0
    while len(raw_messages) < max_messages:
        current_limit = min(page_size, max_messages - len(raw_messages))
        data = run_wx_cli_json(
            args,
            "history",
            chat=chat_ref,
            limit=current_limit,
            offset=offset if kind == "wx" else None,
            cli_resolved=cli_resolved,
            save_raw=False,
        )
        _title, page_messages = wx_cli_message_array(data)
        pages_fetched += 1
        page_summaries.append({"offset": offset, "limit": current_limit, "count": len(page_messages)})
        raw_pages.append({"offset": offset, "limit": current_limit, "count": len(page_messages), "data": data})
        raw_messages.extend(page_messages[:current_limit])

        if kind != "wx" or len(page_messages) < current_limit or not page_messages:
            break
        offset += current_limit

    if getattr(args, "raw_output", None):
        raw_path = expand_path(args.raw_output)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            json.dumps({"chat": chat_ref, "pages": raw_pages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return (
        {"chat": chat_ref, "messages": raw_messages},
        {
            "pages_fetched": pages_fetched,
            "page_size": page_size,
            "max_messages": max_messages,
            "page_summaries": page_summaries,
            "raw_debug": {"page_summaries": page_summaries},
            "warnings": [] if kind == "wx" else ["Pagination is best-effort for non-wx wechat-cli binaries."],
        },
    )


def provider_http_get_json(base_url: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    clean_base = base_url.rstrip("/")
    query = {
        k: v
        for k, v in params.items()
        if v is not None and v != "" and v is not False
    }
    url = f"{clean_base}{endpoint}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, dict) else {"messages": data}


def provider_payload_messages(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("messages", "history", "items", "records", "rows"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key in ("data", "result"):
        nested = data.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            found = provider_payload_messages(nested)
            if found:
                return found
    return []


def provider_payload_sessions(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("sessions", "chatrooms", "chats", "contacts", "items", "rows"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key in ("data", "result"):
        nested = data.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            found = provider_payload_sessions(nested)
            if found:
                return found
    return []


def date_to_provider_epoch(value: str | None, *, exclusive_end: bool = False) -> int | None:
    return parse_date(value, exclusive_end=exclusive_end) if value else None


class WechatProvider:
    name = "provider"
    capabilities: list[str] = []
    default_base_url = ""

    def doctor(self, args: argparse.Namespace) -> dict[str, Any]:
        return {
            "provider": self.name,
            "ok": False,
            "detail": "Provider does not implement doctor()",
            "capabilities": self.capabilities,
        }

    def version(self, args: argparse.Namespace) -> str:
        return ""

    def list_sessions(self, args: argparse.Namespace, limit: int, keyword: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_messages(self, args: argparse.Namespace, chat_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        raise NotImplementedError


class WxCliProvider(WechatProvider):
    name = "wx-cli"
    capabilities = ["sessions", "history", "pagination", "media-paths-best-effort"]

    def doctor(self, args: argparse.Namespace) -> dict[str, Any]:
        try:
            kind, executable = resolve_wx_cli(getattr(args, "cli", "auto"), getattr(args, "binary", None))
            version = self.version(args)
            detail = f"{kind} at {executable}" + (f" ({version})" if version else "")
            ok = True
        except SystemExit as exc:
            detail = str(exc)
            ok = False
        return {
            "provider": self.name,
            "ok": ok,
            "detail": detail,
            "capabilities": self.capabilities,
        }

    def version(self, args: argparse.Namespace) -> str:
        try:
            _kind, executable = resolve_wx_cli(getattr(args, "cli", "auto"), getattr(args, "binary", None))
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return (result.stdout or result.stderr).strip().splitlines()[0] if result.returncode == 0 else ""
        except Exception:
            return ""

    def list_sessions(self, args: argparse.Namespace, limit: int, keyword: str | None = None) -> list[dict[str, Any]]:
        data = run_wx_cli_json(args, "sessions", limit=limit, save_raw=False)
        sessions = [normalize_wx_session(item) for item in extract_wx_sessions(data) if not is_placeholder_wx_session(item)]
        if keyword:
            needle = keyword.lower()
            sessions = [
                item for item in sessions
                if needle in str(item.get("username", "")).lower()
                or needle in str(item.get("display_name", "")).lower()
            ]
        return sessions

    def fetch_messages(self, args: argparse.Namespace, chat_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return fetch_wx_cli_history(args, chat_ref)


class WechatDecryptProvider(WechatProvider):
    name = "wechat-decrypt"
    capabilities = ["http-api", "sessions", "history", "pagination"]
    default_base_url = "http://127.0.0.1:5678"

    def base_url(self, args: argparse.Namespace) -> str:
        return (getattr(args, "base_url", None) or self.default_base_url).rstrip("/")

    def doctor(self, args: argparse.Namespace) -> dict[str, Any]:
        base_url = self.base_url(args)
        try:
            provider_http_get_json(base_url, "/api/history", {"limit": 1, "offset": 0})
            return {
                "provider": self.name,
                "ok": True,
                "detail": f"Reachable at {base_url}",
                "capabilities": self.capabilities,
            }
        except Exception as exc:
            return {
                "provider": self.name,
                "ok": False,
                "detail": (
                    f"Start wechat-decrypt HTTP service, e.g. python main.py, and confirm {base_url}; "
                    f"last error: {exc}"
                ),
                "capabilities": self.capabilities,
            }

    def list_sessions(self, args: argparse.Namespace, limit: int, keyword: str | None = None) -> list[dict[str, Any]]:
        base_url = self.base_url(args)
        last_error = ""
        for endpoint in ("/api/session", "/api/sessions", "/api/recent_sessions", "/api/chatroom", "/api/contact"):
            try:
                data = provider_http_get_json(base_url, endpoint, {"limit": limit, "keyword": keyword or ""})
            except Exception as exc:
                last_error = str(exc)
                continue
            sessions = provider_payload_sessions(data)
            if sessions:
                normalized = [normalize_wx_session(item) for item in sessions if not is_placeholder_wx_session(item)]
                if keyword:
                    needle = keyword.lower()
                    normalized = [
                        item for item in normalized
                        if needle in str(item.get("username", "")).lower()
                        or needle in str(item.get("display_name", "")).lower()
                    ]
                return normalized[:limit]
        if last_error:
            warn(f"wechat-decrypt session endpoints unavailable: {last_error}")
        return []

    def fetch_messages(self, args: argparse.Namespace, chat_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        base_url = self.base_url(args)
        max_messages = int(getattr(args, "max_messages", None) or getattr(args, "limit", 5000))
        page_size = int(getattr(args, "page_size", None) or min(max_messages, 500))
        if max_messages <= 0:
            die("--max-messages/--limit must be greater than 0")
        if page_size <= 0:
            die("--page-size must be greater than 0")

        raw_messages: list[dict[str, Any]] = []
        raw_pages: list[dict[str, Any]] = []
        page_summaries: list[dict[str, Any]] = []
        offset = 0
        pages_fetched = 0
        while len(raw_messages) < max_messages:
            current_limit = min(page_size, max_messages - len(raw_messages))
            params = {
                "chat": chat_ref,
                "talker": chat_ref,
                "limit": current_limit,
                "offset": offset,
                "since": date_to_provider_epoch(getattr(args, "since", None)),
                "until": date_to_provider_epoch(getattr(args, "until", None), exclusive_end=True),
                "format": "json",
            }
            data = provider_http_get_json(base_url, "/api/history", params)
            page_messages = provider_payload_messages(data)
            pages_fetched += 1
            page_summaries.append({"offset": offset, "limit": current_limit, "count": len(page_messages)})
            raw_pages.append({"offset": offset, "limit": current_limit, "count": len(page_messages), "data": data})
            raw_messages.extend(page_messages[:current_limit])
            if len(page_messages) < current_limit or not page_messages:
                break
            offset += current_limit

        if getattr(args, "raw_output", None):
            raw_path = expand_path(args.raw_output)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                json.dumps({"chat": chat_ref, "pages": raw_pages}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return (
            {"chat": chat_ref, "messages": raw_messages},
            {
                "pages_fetched": pages_fetched,
                "page_size": page_size,
                "max_messages": max_messages,
                "page_summaries": page_summaries,
                "raw_debug": {"page_summaries": page_summaries},
                "warnings": [],
            },
        )


class WechatMcpMacosProvider(WechatProvider):
    name = "wechat-mcp-macos"
    capabilities = ["mcp", "sessions", "history", "search", "detected-but-not-supported"]

    def doctor(self, args: argparse.Namespace) -> dict[str, Any]:
        executable = shutil.which("wechat-mcp-macos")
        config = Path.home() / ".wechat-mcp" / "config.json"
        if executable and config.exists():
            detail = (
                f"Detected {executable} and {config}; direct stdio MCP import is not implemented yet. "
                "Use this doctor result to verify setup before wiring an MCP client."
            )
            ok = False
        elif executable:
            detail = f"Detected {executable}, but {config} is missing; run wechat-mcp-macos setup first."
            ok = False
        else:
            detail = "wechat-mcp-macos command not found. Install with: uv tool install wechat-mcp-macos"
            ok = False
        return {
            "provider": self.name,
            "ok": ok,
            "detail": detail,
            "capabilities": self.capabilities,
        }

    def list_sessions(self, args: argparse.Namespace, limit: int, keyword: str | None = None) -> list[dict[str, Any]]:
        die("wechat-mcp-macos provider is detected but direct MCP stdio import is not implemented yet.")

    def fetch_messages(self, args: argparse.Namespace, chat_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
        die("wechat-mcp-macos provider is detected but direct MCP stdio import is not implemented yet.")


def get_wechat_providers() -> dict[str, WechatProvider]:
    providers: list[WechatProvider] = [
        WxCliProvider(),
        WechatDecryptProvider(),
        WechatMcpMacosProvider(),
    ]
    return {provider.name: provider for provider in providers}


def resolve_provider_session(
    provider: WechatProvider,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any], list[str]]:
    chat_id = getattr(args, "chat_id", None) or None
    chat_name = getattr(args, "chat_name", None) or None
    legacy_chat = getattr(args, "chat", None) or None
    if not chat_id and not chat_name and legacy_chat:
        if looks_like_chat_id(legacy_chat):
            chat_id = legacy_chat
        else:
            chat_name = legacy_chat
    if not chat_id and not chat_name:
        die("--chat-id, --chat-name, or --chat is required")

    raw_ref = chat_id or chat_name or legacy_chat or ""
    warnings: list[str] = []
    if not getattr(args, "resolve_chat", True):
        warnings.append("Chat resolution disabled; importing with the raw chat reference.")
        return raw_ref, minimal_wx_session(raw_ref), warnings

    sessions = provider.list_sessions(args, getattr(args, "session_limit", 500), chat_name)
    if not sessions:
        if chat_id:
            warnings.append(f"{provider.name} returned no usable sessions; importing the exact --chat-id without metadata.")
            return chat_id, minimal_wx_session(chat_id), warnings
        die(f"{provider.name} returned no usable sessions; pass --chat-id with --no-resolve-chat to import a known id.")
    resolved = resolve_wx_session(sessions, chat_id=chat_id, chat_name=chat_name)
    if chat_id and not resolved.get("matched"):
        warnings.append(f"Exact --chat-id was not present in {provider.name} sessions output; metadata is incomplete.")
    return str(resolved["username"]), resolved, warnings


def provider_manifest_session(resolved_session: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": resolved_session.get("username", ""),
        "display_name": resolved_session.get("display_name", ""),
        "chat_type": resolved_session.get("chat_type", ""),
        "is_group": bool(resolved_session.get("is_group")),
        "matched": bool(resolved_session.get("matched")),
        "last_message_at": resolved_session.get("last_message_at", ""),
    }


def import_with_provider(
    args: argparse.Namespace,
    provider: WechatProvider,
    *,
    manifest_name: str,
) -> dict[str, Any]:
    chat_ref, resolved_session, resolve_warnings = resolve_provider_session(provider, args)
    data, fetch_audit = provider.fetch_messages(args, chat_ref)
    source_name = str(resolved_session.get("display_name") or chat_ref)
    title, messages, audit = normalize_wx_cli_payload_with_audit(
        data,
        source_name,
        getattr(args, "since", None),
        getattr(args, "until", None),
        fetch_audit,
    )
    audit["warnings"].extend(resolve_warnings)
    audit.update({
        "provider": provider.name,
        "provider_version": provider.version(args),
        "provider_capabilities": provider.capabilities,
        "resolved_session": provider_manifest_session(resolved_session),
    })

    return export_weflow_messages(
        messages,
        args.title
        or args.subfolder
        or str(resolved_session.get("display_name") or "")
        or title
        or chat_ref,
        expand_path(args.vault),
        args.folder,
        args.subfolder or safe_segment(str(resolved_session.get("display_name") or title)),
        args.mode,
        None,
        None,
        source_json=None,
        copy_media=not args.no_media_copy,
        source_label=provider.name,
        manifest_name=manifest_name,
        manifest_extra=audit,
    )


def provider_doctor_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    providers = get_wechat_providers()
    selected = getattr(args, "provider", "all")
    if selected != "all" and selected not in providers:
        die(f"Unknown provider {selected!r}; available: {', '.join(sorted(providers))}")
    names = sorted(providers) if selected == "all" else [selected]
    return [providers[name].doctor(args) for name in names]


def cmd_providers(args: argparse.Namespace) -> int:
    rows = provider_doctor_rows(args)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    width = max(len(row["provider"]) for row in rows)
    for row in rows:
        status = "OK" if row.get("ok") else "MISS"
        caps = ",".join(row.get("capabilities", []))
        print(f"{row['provider']:<{width}}  {status:<4}  {caps}  {row.get('detail', '')}")
    return 0


def cmd_provider_doctor(args: argparse.Namespace) -> int:
    return cmd_providers(args)


def cmd_import_wechat(args: argparse.Namespace) -> int:
    providers = get_wechat_providers()
    provider = providers.get(args.provider)
    if not provider:
        die(f"Unknown provider {args.provider!r}; available: {', '.join(sorted(providers))}")
    manifest = import_with_provider(args, provider, manifest_name="_wechat_import_manifest.json")
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        info(f"Imported {manifest['message_count']} messages from {provider.name}")
        info(f"day_files_written={manifest['day_files_written']} skipped={manifest['day_files_skipped']}")
        info(f"output={manifest['output']}")
    return 0


def cmd_wx_sessions(args: argparse.Namespace) -> int:
    data = run_wx_cli_json(args, "sessions")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    sessions = extract_wx_sessions(data)
    if not sessions:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"{'unread':>8}  {'id/username':<42}  name")
    print("-" * 92)
    for item in sessions:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("username") or item.get("id") or item.get("chat") or "")
        name = str(item.get("chat") or item.get("name") or item.get("display_name") or item.get("summary") or "")
        unread = item.get("unread", "")
        if len(sid) > 42:
            sid = sid[:39] + "..."
        print(f"{str(unread):>8}  {sid:<42}  {name}")
    return 0


def cmd_import_wx_cli(args: argparse.Namespace) -> int:
    resolved_session: dict[str, Any]
    fetch_audit: dict[str, Any] = {}
    resolve_warnings: list[str] = []
    if args.input_json:
        input_path = expand_path(args.input_json)
        if not input_path.exists():
            die(f"wx-cli JSON not found: {input_path}")
        data = load_json_maybe_wrapped(input_path.read_text(encoding="utf-8"))
        source_json: Path | None = input_path
        source_name = input_path.stem
        preview_title, _preview_messages = wx_cli_message_array(data)
        resolved_session = minimal_wx_session(preview_title if preview_title != "wx-cli" else source_name)
    else:
        chat_ref, resolved_session, resolve_warnings = resolve_import_wx_session(args)
        data, fetch_audit = fetch_wx_cli_history(args, chat_ref)
        source_json = None
        source_name = str(resolved_session.get("display_name") or chat_ref)

    title, messages, audit = normalize_wx_cli_payload_with_audit(
        data,
        source_name,
        args.since,
        args.until,
        fetch_audit,
    )
    audit["warnings"].extend(resolve_warnings)
    audit["resolved_session"] = {
        "username": resolved_session.get("username", ""),
        "display_name": resolved_session.get("display_name", ""),
        "chat_type": resolved_session.get("chat_type", ""),
        "is_group": bool(resolved_session.get("is_group")),
        "matched": bool(resolved_session.get("matched")),
        "last_message_at": resolved_session.get("last_message_at", ""),
    }
    audit.update({
        "provider": "wx-cli",
        "provider_version": WxCliProvider().version(args),
        "provider_capabilities": WxCliProvider.capabilities,
    })
    manifest = export_weflow_messages(
        messages,
        args.title
        or args.subfolder
        or str(resolved_session.get("display_name") or "")
        or title
        or getattr(args, "chat", None)
        or "wx-cli",
        expand_path(args.vault),
        args.folder,
        args.subfolder or safe_segment(str(resolved_session.get("display_name") or title)),
        args.mode,
        None,
        None,
        source_json=source_json,
        copy_media=not args.no_media_copy,
        source_label="wx-cli",
        manifest_name="_wx_cli_import_manifest.json",
        manifest_extra=audit,
    )
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        info(f"Imported {manifest['message_count']} messages from wx-cli")
        info(f"day_files_written={manifest['day_files_written']} skipped={manifest['day_files_skipped']}")
        info(f"output={manifest['output']}")
    return 0


def cmd_weflow_sessions(args: argparse.Namespace) -> int:
    base_url, token = resolve_weflow_api_options(args)
    payload = weflow_api_get(
        base_url,
        "/api/v1/sessions",
        {
            "keyword": args.keyword,
            "limit": args.limit,
            "format": "chatlab" if args.chatlab else "",
        },
        token,
    )
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        die("WeFlow API response does not contain sessions[]")
    if args.json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return 0
    print(f"{'messages':>8}  {'id/username':<42}  name")
    print("-" * 92)
    for item in sessions:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or item.get("username") or "")
        name = str(item.get("name") or item.get("displayName") or "")
        count = item.get("messageCount", "")
        if not count and item.get("lastTimestamp"):
            count = "-"
        if len(sid) > 42:
            sid = sid[:39] + "..."
        print(f"{str(count):>8}  {sid:<42}  {name}")
    return 0


def parse_date(value: str, *, exclusive_end: bool = False) -> int:
    try:
        when = dt.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        die(f"Invalid date {value!r}; expected YYYY-MM-DD")
    if exclusive_end:
        when += dt.timedelta(days=1)
    return int(when.timestamp())


def safe_segment(value: str) -> str:
    return re.sub(r"[/:\\]+", "_", value).strip() or "export"


def safe_vault_path(vault: Path, *parts: str) -> Path:
    vault = vault.expanduser().resolve()
    if not vault.exists() or not vault.is_dir():
        die(f"Obsidian vault does not exist or is not a directory: {vault}")

    cleaned: list[Path] = []
    for part in parts:
        subpath = Path(part)
        if subpath.is_absolute() or any(piece == ".." for piece in subpath.parts):
            die(f"Unsafe vault path segment: {part}")
        cleaned.append(subpath)

    output = vault.joinpath(*cleaned).resolve()
    try:
        output.relative_to(vault)
    except ValueError:
        die(f"Output path escapes vault: {output}")
    return output


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({validate_msg_table_name(table)})")
    return [to_text(row[1]) for row in cur.fetchall()]


def sender_map(con: sqlite3.Connection) -> dict[int, str]:
    if not table_exists(con, "Name2Id"):
        return {}
    cur = con.cursor()
    columns = [to_text(row[1]) for row in cur.execute("PRAGMA table_info(Name2Id)").fetchall()]
    if "user_name" not in columns:
        return {}
    cur.execute("SELECT rowid, user_name FROM Name2Id")
    return {int(row[0]): to_text(row[1]) for row in cur.fetchall()}


def query_messages(
    con: sqlite3.Connection,
    table: str,
    since: int | None,
    until: int | None,
) -> tuple[list[dict[str, Any]], bool]:
    columns = table_columns(con, table)
    required = {"local_id", "local_type", "create_time", "message_content"}
    missing = required - set(columns)
    if missing:
        die(f"{table} is missing required columns: {sorted(missing)}")

    include_sender = "real_sender_id" in columns
    select_cols = ["local_id", "local_type", "create_time", "CAST(message_content AS BLOB)"]
    if include_sender:
        select_cols.append("real_sender_id")

    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("create_time >= ?")
        params.append(since)
    if until is not None:
        where.append("create_time < ?")
        params.append(until)

    sql = f"SELECT {', '.join(select_cols)} FROM {validate_msg_table_name(table)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY create_time ASC, local_id ASC"

    cur = con.cursor()
    cur.execute(sql, params)
    rows: list[dict[str, Any]] = []
    for row in cur.fetchall():
        item = {
            "local_id": int(row[0]),
            "local_type": int(row[1]),
            "create_time": int(row[2]),
            "content": row[3],
            "real_sender_id": int(row[4]) if include_sender and row[4] is not None else None,
        }
        rows.append(item)
    return rows, include_sender


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_day_file(
    path: Path,
    target: str,
    day: str,
    messages: list[dict[str, Any]],
    senders: dict[int, str],
    include_senders: bool,
    mode: str,
) -> bool:
    if mode == "skip" and path.exists():
        return False

    exported_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "---",
        "source: wechat",
        f"target: {yaml_string(target)}",
        f"date: {yaml_string(day)}",
        f"message_count: {len(messages)}",
        f"exported_at: {yaml_string(exported_at)}",
        "---",
        "",
        f"# {day} · {target}",
        "",
    ]

    for msg in messages:
        when = dt.datetime.fromtimestamp(msg["create_time"])
        tag = TYPE_MAP.get(msg["local_type"], f"type{msg['local_type']}")
        lines.append(f"## {when.strftime('%H:%M:%S')} · {tag}")
        lines.append("")
        if include_senders and msg.get("real_sender_id") is not None:
            sender = senders.get(msg["real_sender_id"], str(msg["real_sender_id"]))
            lines.append(f"`sender`: {sender}")
            lines.append("")
        body = format_message(msg["local_type"], msg["content"]).strip()
        lines.append(body if body else "_(empty)_")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True


def copy_attachments(
    user_dir: Path,
    target_hash: str,
    month: str,
    dst: Path,
    max_bytes: int,
) -> dict[str, Any]:
    src = user_dir / "msg" / "attach" / target_hash / month
    manifest = {"month": month, "source": str(src), "copied": [], "skipped": []}
    if not src.exists():
        return manifest

    for root, _, files in os.walk(src):
        for name in files:
            if name.startswith("."):
                continue
            sp = Path(root) / name
            try:
                size = sp.stat().st_size
            except OSError as exc:
                manifest["skipped"].append({"path": str(sp), "reason": str(exc)})
                continue
            if size > max_bytes:
                manifest["skipped"].append({"path": str(sp), "reason": f"larger than {max_bytes} bytes"})
                continue
            rel = sp.relative_to(src)
            dp = dst / rel
            dp.parent.mkdir(parents=True, exist_ok=True)
            if not dp.exists():
                shutil.copy2(sp, dp)
            manifest["copied"].append({"source": str(sp), "dest": str(dp), "bytes": size})
    return manifest


def cmd_export_chat(args: argparse.Namespace) -> int:
    con = connect_db(expand_path(args.db))
    target_hash = hashlib.md5(args.target.encode("utf-8")).hexdigest()
    table = validate_msg_table_name("Msg_" + target_hash)
    if not table_exists(con, table):
        die(f"Table {table} not found. Run list-targets and use the exact target id.")

    since = parse_date(args.since) if args.since else None
    until = parse_date(args.until, exclusive_end=True) if args.until else None
    if since and until and since >= until:
        die("--since must be earlier than --until")

    messages, has_sender_column = query_messages(con, table, since, until)
    senders = sender_map(con) if args.with_senders and has_sender_column else {}

    subfolder = args.subfolder or safe_segment(args.target)
    out_root = safe_vault_path(expand_path(args.vault), args.folder, subfolder)
    out_root.mkdir(parents=True, exist_ok=True)

    by_day: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        day = dt.datetime.fromtimestamp(msg["create_time"]).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(msg)

    user_dir = None
    if not args.no_attachments:
        user_dir = expand_path(args.wechat_root) if args.wechat_root else pick_user_dir()

    written = 0
    skipped = 0
    attachment_manifests: list[dict[str, Any]] = []
    copied_months: set[str] = set()
    for day in sorted(by_day):
        month = day[:7]
        month_dir = out_root / month
        if user_dir and month not in copied_months:
            manifest = copy_attachments(
                user_dir,
                target_hash,
                month,
                month_dir / "attachments",
                args.max_attachment_mb * 1024 * 1024,
            )
            attachment_manifests.append(manifest)
            copied_months.add(month)

        if write_day_file(
            month_dir / f"{day}.md",
            args.target,
            day,
            by_day[day],
            senders,
            args.with_senders and has_sender_column,
            args.mode,
        ):
            written += 1
        else:
            skipped += 1

    manifest = {
        "source": "wechat",
        "target": args.target,
        "target_hash": target_hash,
        "db": str(expand_path(args.db)),
        "output": str(out_root),
        "exported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "message_count": len(messages),
        "day_files_written": written,
        "day_files_skipped": skipped,
        "attachments": attachment_manifests,
    }
    (out_root / "_export_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        info(f"target={args.target} hash={target_hash}")
        info(f"messages={len(messages)} day_files_written={written} skipped={skipped}")
        info(f"output={out_root}")
        copied = sum(len(item["copied"]) for item in attachment_manifests)
        if copied:
            info(f"attachments_copied={copied}")
    con.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export WeChat for macOS 4.x data to Obsidian")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check local requirements")
    doctor.add_argument("--wechat-app", help="Path to WeChat.app or signed copy")
    doctor.add_argument("--base", help="Override xwechat_files base path")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable status")
    doctor.set_defaults(func=cmd_doctor)

    locate = sub.add_parser("locate-user", help="Locate the active WeChat user directory")
    locate.add_argument("--base", help="Override xwechat_files base path")
    locate.add_argument("--print-path", action="store_true", help="Print only the path")
    locate.add_argument("--json", action="store_true", help="Print JSON")
    locate.set_defaults(func=cmd_locate_user)

    sign = sub.add_parser("sign-wechat", help="Copy and ad-hoc sign WeChat.app")
    sign.add_argument("--source", help="Source WeChat.app path; auto-detected if omitted")
    sign.add_argument("--dest", default="~/Desktop/WeChat-Obsidian.app", help="Destination app path")
    sign.add_argument("--force", action="store_true", help="Replace destination if it exists")
    sign.set_defaults(func=cmd_sign_wechat)

    capture = sub.add_parser("capture-keys", help="Capture SQLCipher derived keys with Frida")
    capture.add_argument("--wechat-app", default="~/Desktop/WeChat-Obsidian.app", help="Signed WeChat.app path")
    capture.add_argument("--out", default=str(DEFAULT_KEYS_LOG), help="Key log path")
    capture.add_argument("--append", action="store_true", help="Append to an existing key log")
    capture.add_argument("--wait", type=int, default=300, help="Seconds to keep hook active")
    capture.add_argument("--attach-timeout", type=int, default=120, help="Seconds to wait for WeChat process")
    capture.add_argument("--mode", choices=["attach", "spawn"], default="attach", help="Frida attach or spawn mode")
    capture.add_argument("--launch", action="store_true", help="Launch the signed app before attaching")
    capture.set_defaults(func=cmd_capture_keys)

    decrypt = sub.add_parser("decrypt", help="Decrypt a WeChat SQLCipher database")
    decrypt.add_argument("--db", required=True, help="Encrypted database path")
    decrypt.add_argument("--out", required=True, help="Output plaintext SQLite path")
    decrypt.add_argument("--key", help="64-char hex encryption key; otherwise matched from --keys-log")
    decrypt.add_argument("--keys-log", default=str(DEFAULT_KEYS_LOG), help="Captured key log")
    decrypt.add_argument("--no-verify", action="store_true", help="Skip SQLite verification")
    decrypt.set_defaults(func=cmd_decrypt)

    targets = sub.add_parser("list-targets", help="List exportable conversation targets")
    targets.add_argument("--db", required=True, help="Decrypted message_0.db path")
    targets.add_argument("--limit", type=int, default=100, help="Maximum rows to print")
    targets.add_argument("--json", action="store_true", help="Print JSON")
    targets.set_defaults(func=cmd_list_targets)

    export = sub.add_parser("export-chat", help="Export one conversation to an Obsidian vault")
    export.add_argument("--db", required=True, help="Decrypted message_0.db path")
    export.add_argument("--target", required=True, help="filehelper, wxid_*, or *@chatroom target id")
    export.add_argument("--vault", required=True, help="Obsidian vault root")
    export.add_argument("--folder", default="聊天记录导出", help="Folder inside the vault")
    export.add_argument("--subfolder", help="Subfolder inside --folder; defaults to a safe target id")
    export.add_argument("--wechat-root", help="WeChat wxid_* user directory for attachments")
    export.add_argument("--no-attachments", action="store_true", help="Skip attachment copying")
    export.add_argument("--max-attachment-mb", type=int, default=200, help="Per-file attachment copy cap")
    export.add_argument("--with-senders", action="store_true", help="Include sender ids when available")
    export.add_argument("--since", help="Inclusive start date, YYYY-MM-DD")
    export.add_argument("--until", help="Inclusive end date, YYYY-MM-DD")
    export.add_argument("--mode", choices=["overwrite", "skip"], default="overwrite", help="Daily file write mode")
    export.add_argument("--json", action="store_true", help="Print JSON summary")
    export.set_defaults(func=cmd_export_chat)

    weflow_json = sub.add_parser("import-weflow-json", help="Import a WeFlow JSON export into Obsidian")
    weflow_json.add_argument("--input", required=True, help="WeFlow JSON export path")
    weflow_json.add_argument("--vault", required=True, help="Obsidian vault root")
    weflow_json.add_argument("--folder", default="聊天记录导出", help="Folder inside the vault")
    weflow_json.add_argument("--subfolder", help="Subfolder inside --folder; defaults to session title")
    weflow_json.add_argument("--title", help="Override note title")
    weflow_json.add_argument("--since", help="Inclusive start date, YYYY-MM-DD")
    weflow_json.add_argument("--until", help="Inclusive end date, YYYY-MM-DD")
    weflow_json.add_argument("--mode", choices=["overwrite", "skip"], default="overwrite", help="Daily file write mode")
    weflow_json.add_argument("--no-media-copy", action="store_true", help="Reference media in place instead of copying files")
    weflow_json.add_argument("--json", action="store_true", help="Print JSON summary")
    weflow_json.set_defaults(func=cmd_import_weflow_json)

    weflow_sessions = sub.add_parser("weflow-sessions", help="List sessions from the WeFlow local HTTP API")
    weflow_sessions.add_argument("--base-url", help="WeFlow API base URL; defaults to WeFlow config or http://127.0.0.1:5031")
    weflow_sessions.add_argument("--token", help="WeFlow API access token; defaults to WEFLOW_TOKEN or WeFlow config")
    weflow_sessions.add_argument("--config", help="WeFlow-config.json path")
    weflow_sessions.add_argument("--keyword", help="Filter sessions by username or display name")
    weflow_sessions.add_argument("--limit", type=int, default=100, help="Maximum sessions")
    weflow_sessions.add_argument("--chatlab", action="store_true", help="Request ChatLab session format")
    weflow_sessions.add_argument("--json", action="store_true", help="Print JSON")
    weflow_sessions.set_defaults(func=cmd_weflow_sessions)

    weflow_api = sub.add_parser("import-weflow-api", help="Import messages from the WeFlow local HTTP API")
    weflow_api.add_argument("--talker", required=True, help="WeFlow/WeChat session id, e.g. filehelper, wxid_*, or *@chatroom")
    weflow_api.add_argument("--vault", required=True, help="Obsidian vault root")
    weflow_api.add_argument("--folder", default="聊天记录导出", help="Folder inside the vault")
    weflow_api.add_argument("--subfolder", help="Subfolder inside --folder; defaults to talker")
    weflow_api.add_argument("--title", help="Override note title")
    weflow_api.add_argument("--base-url", help="WeFlow API base URL; defaults to WeFlow config or http://127.0.0.1:5031")
    weflow_api.add_argument("--token", help="WeFlow API access token; defaults to WEFLOW_TOKEN or WeFlow config")
    weflow_api.add_argument("--config", help="WeFlow-config.json path")
    weflow_api.add_argument("--since", help="Inclusive start date, YYYY-MM-DD")
    weflow_api.add_argument("--until", help="Inclusive end date, YYYY-MM-DD")
    weflow_api.add_argument("--limit", type=int, default=10000, help="Page size, max usually 10000")
    weflow_api.add_argument("--api-mode", choices=["messages", "chatlab"], default="chatlab", help="Use WeFlow raw messages API or ChatLab pull API")
    weflow_api.add_argument("--media", action="store_true", help="Ask WeFlow API to export/return media")
    weflow_api.add_argument("--mode", choices=["overwrite", "skip"], default="overwrite", help="Daily file write mode")
    weflow_api.add_argument("--no-media-copy", action="store_true", help="Reference media in place instead of copying files")
    weflow_api.add_argument("--json", action="store_true", help="Print JSON summary")
    weflow_api.set_defaults(func=cmd_import_weflow_api)

    providers = sub.add_parser("providers", help="List available WeChat reader providers and readiness")
    providers.add_argument("--provider", choices=["all", "wx-cli", "wechat-decrypt", "wechat-mcp-macos"], default="all", help="Provider to inspect")
    providers.add_argument("--base-url", default="http://127.0.0.1:5678", help="HTTP provider base URL")
    providers.add_argument("--cli", choices=["auto", "wx", "wechat-cli"], default="auto", help="wx-cli command to inspect")
    providers.add_argument("--binary", help="Explicit wx/wechat-cli binary path")
    providers.add_argument("--json", action="store_true", help="Print JSON")
    providers.set_defaults(func=cmd_providers)

    provider_doctor = sub.add_parser("provider-doctor", help="Run provider readiness checks")
    provider_doctor.add_argument("--provider", choices=["all", "wx-cli", "wechat-decrypt", "wechat-mcp-macos"], default="all", help="Provider to inspect")
    provider_doctor.add_argument("--base-url", default="http://127.0.0.1:5678", help="HTTP provider base URL")
    provider_doctor.add_argument("--cli", choices=["auto", "wx", "wechat-cli"], default="auto", help="wx-cli command to inspect")
    provider_doctor.add_argument("--binary", help="Explicit wx/wechat-cli binary path")
    provider_doctor.add_argument("--json", action="store_true", help="Print JSON")
    provider_doctor.set_defaults(func=cmd_provider_doctor)

    import_wechat = sub.add_parser("import-wechat", help="Import messages from a pluggable WeChat reader provider")
    import_wechat.add_argument("--provider", choices=["wx-cli", "wechat-decrypt", "wechat-mcp-macos"], default="wx-cli", help="Reader provider")
    import_wechat.add_argument("--chat", help="Backward-compatible chat/group name or id; names are resolved before import")
    import_wechat.add_argument("--chat-id", help="Exact provider chat id, e.g. filehelper, wxid_*, or *@chatroom")
    import_wechat.add_argument("--chat-name", help="Display name to resolve before import")
    import_wechat.add_argument("--base-url", default="http://127.0.0.1:5678", help="HTTP provider base URL")
    import_wechat.add_argument("--cli", choices=["auto", "wx", "wechat-cli"], default="auto", help="wx-cli command to use")
    import_wechat.add_argument("--binary", help="Explicit wx/wechat-cli binary path")
    import_wechat.add_argument("--vault", required=True, help="Obsidian vault root")
    import_wechat.add_argument("--folder", default="聊天记录导出", help="Folder inside the vault")
    import_wechat.add_argument("--subfolder", help="Subfolder inside --folder; defaults to chat title")
    import_wechat.add_argument("--title", help="Override note title")
    import_wechat.add_argument("--since", help="Inclusive start date, YYYY-MM-DD")
    import_wechat.add_argument("--until", help="Inclusive end date, YYYY-MM-DD")
    import_wechat.add_argument("--limit", type=int, default=5000, help="Backward-compatible maximum messages to read")
    import_wechat.add_argument("--max-messages", type=int, help="Maximum messages to fetch across all pages")
    import_wechat.add_argument("--page-size", type=int, default=500, help="Messages to request per provider page")
    import_wechat.add_argument("--session-limit", type=int, default=500, help="Maximum session rows used while resolving chat names")
    import_wechat.add_argument("--resolve-chat", dest="resolve_chat", action="store_true", default=True, help="Resolve names through provider sessions before import")
    import_wechat.add_argument("--no-resolve-chat", dest="resolve_chat", action="store_false", help="Use the raw --chat/--chat-id value without session resolution")
    import_wechat.add_argument("--media", action="store_true", help="Ask provider to resolve media paths when supported")
    import_wechat.add_argument("--mode", choices=["overwrite", "skip"], default="overwrite", help="Daily file write mode")
    import_wechat.add_argument("--no-media-copy", action="store_true", help="Reference media in place instead of copying files")
    import_wechat.add_argument("--raw-output", help="Save raw provider output")
    import_wechat.add_argument("--json", action="store_true", help="Print JSON summary")
    import_wechat.set_defaults(func=cmd_import_wechat)

    wx_sessions = sub.add_parser("wx-sessions", help="List sessions from jackwener/wx-cli or local wechat-cli")
    wx_sessions.add_argument("--cli", choices=["auto", "wx", "wechat-cli"], default="auto", help="CLI command to use")
    wx_sessions.add_argument("--binary", help="Explicit wx/wechat-cli binary path")
    wx_sessions.add_argument("--limit", type=int, default=100, help="Maximum sessions")
    wx_sessions.add_argument("--raw-output", help="Save raw CLI JSON output")
    wx_sessions.add_argument("--json", action="store_true", help="Print JSON")
    wx_sessions.set_defaults(func=cmd_wx_sessions)

    wx_cli = sub.add_parser("import-wx-cli", help="Import messages from jackwener/wx-cli or local wechat-cli into Obsidian")
    wx_cli.add_argument("--chat", help="Backward-compatible chat/group name or id; names are resolved before import")
    wx_cli.add_argument("--chat-id", help="Exact wx-cli username/id, e.g. filehelper, wxid_*, or *@chatroom")
    wx_cli.add_argument("--chat-name", help="Display name to resolve through wx-sessions before import")
    wx_cli.add_argument("--input-json", help="Existing wx-cli/wechat-cli history JSON file")
    wx_cli.add_argument("--cli", choices=["auto", "wx", "wechat-cli"], default="auto", help="CLI command to use")
    wx_cli.add_argument("--binary", help="Explicit wx/wechat-cli binary path")
    wx_cli.add_argument("--vault", required=True, help="Obsidian vault root")
    wx_cli.add_argument("--folder", default="聊天记录导出", help="Folder inside the vault")
    wx_cli.add_argument("--subfolder", help="Subfolder inside --folder; defaults to chat title")
    wx_cli.add_argument("--title", help="Override note title")
    wx_cli.add_argument("--since", help="Inclusive start date, YYYY-MM-DD")
    wx_cli.add_argument("--until", help="Inclusive end date, YYYY-MM-DD")
    wx_cli.add_argument("--limit", type=int, default=5000, help="Backward-compatible maximum messages to read")
    wx_cli.add_argument("--max-messages", type=int, help="Maximum messages to fetch across all wx-cli pages")
    wx_cli.add_argument("--page-size", type=int, default=500, help="Messages to request per wx history page")
    wx_cli.add_argument("--session-limit", type=int, default=500, help="Maximum wx-sessions rows used while resolving chat names")
    wx_cli.add_argument("--resolve-chat", dest="resolve_chat", action="store_true", default=True, help="Resolve names through wx-sessions before import")
    wx_cli.add_argument("--no-resolve-chat", dest="resolve_chat", action="store_false", help="Use the raw --chat/--chat-id value without wx-sessions resolution")
    wx_cli.add_argument("--media", action="store_true", help="Ask CLI to resolve media paths when supported")
    wx_cli.add_argument("--mode", choices=["overwrite", "skip"], default="overwrite", help="Daily file write mode")
    wx_cli.add_argument("--no-media-copy", action="store_true", help="Reference media in place instead of copying files")
    wx_cli.add_argument("--raw-output", help="Save raw CLI JSON output")
    wx_cli.add_argument("--json", action="store_true", help="Print JSON summary")
    wx_cli.set_defaults(func=cmd_import_wx_cli)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
