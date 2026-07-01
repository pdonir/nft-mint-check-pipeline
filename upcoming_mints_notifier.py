#!/usr/bin/env python3
"""Telegram command notifier for upcoming_mints.json.

Commands:
  /upcoming <wallet/account>
  /today <wallet/account>
  /slug <project/link/slug>[, <project2>] [--wallet <wallet/account>[, <wallet2>]]
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
UPCOMING_FILE = BASE_DIR / "upcoming_mints.json"
WALLETS_FILE = BASE_DIR / "wallets.json"
STATE_FILE = BASE_DIR / ".upcoming_mints_notifier_state.json"
LOG_FILE = BASE_DIR / "upcoming_mints_notifier.log"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_MESSAGE_THREAD_ID = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
UPCOMING_MINTS_THREAD_ID = os.environ.get("UPCOMING_MINTS_THREAD_ID", TELEGRAM_MESSAGE_THREAD_ID)
UPCOMING_MINTS_ALLOWED_THREAD_IDS = os.environ.get("UPCOMING_MINTS_ALLOWED_THREAD_IDS", UPCOMING_MINTS_THREAD_ID)
POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30"))
MAX_RESULTS = int(os.environ.get("UPCOMING_NOTIFIER_MAX_RESULTS", "12"))
ADMIN_USER_IDS = {item.strip() for item in os.environ.get("UPCOMING_NOTIFIER_ADMIN_USER_IDS", "").split(",") if item.strip()}
PIPELINE_COMMAND = os.environ.get("UPCOMING_PIPELINE_COMMAND", "python3 nft_mint_check.py")
PIPELINE_TIMEOUT = int(os.environ.get("UPCOMING_PIPELINE_TIMEOUT", "1800"))
PIPELINE_LOG_FILE = Path(os.environ.get("UPCOMING_PIPELINE_LOG_FILE", str(BASE_DIR / "upcoming_pipeline_manual.log")))
PIPELINE_LOCK = threading.Lock()
PIPELINE_RUNNING = False


def _detect_local_tz():
    try:
        offset = datetime.now(timezone.utc).astimezone().utcoffset()
        if offset and offset.total_seconds() != 0:
            return timezone(offset)
    except Exception:
        pass
    return timezone(timedelta(hours=7))


LOCAL_TZ = _detect_local_tz()
LOCAL_TZ_OFFSET = LOCAL_TZ.utcoffset(datetime.now()) or timedelta(hours=7)
LOCAL_TZ_NAME = f"GMT+{int(LOCAL_TZ_OFFSET.total_seconds() // 3600)}"


def log(message: str) -> None:
    line = f"[{datetime.now(LOCAL_TZ).isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception as exc:
        log(f"failed to load {path}: {exc}")
    return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(path)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def load_wallet_display() -> dict[str, str]:
    data = load_json(WALLETS_FILE, {})
    if isinstance(data, dict):
        return {str(k): str(v.get("display", k)) if isinstance(v, dict) else str(k) for k, v in data.items()}
    if isinstance(data, list):
        return {str(k): str(k) for k in data}
    return {}


def is_stage_today(stage: str) -> bool:
    now = datetime.now(LOCAL_TZ)
    m = re.search(r"—\s*(\d{1,2})\s+(\w+)\s+\d{2}:\d{2}\s+GMT[+-]\d+", stage or "")
    if not m:
        return False
    try:
        month = datetime.strptime(m.group(2), "%b").month
    except ValueError:
        return False
    return int(m.group(1)) == now.day and month == now.month


def parse_stage_time(stage: str):
    m = re.search(r"—\s*(\d{1,2}\s+\w+\s+\d{2}:\d{2})\s+GMT[+-]\d+", stage or "")
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)} {datetime.now(LOCAL_TZ).year}", "%d %b %H:%M %Y")
        return dt.replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def extract_earliest_time(entry: dict[str, Any]):
    earliest = None
    for stages in entry.get("wallets", {}).values():
        if not isinstance(stages, list):
            continue
        for stage in stages:
            dt = parse_stage_time(stage)
            if dt and (earliest is None or dt < earliest):
                earliest = dt
    return earliest or datetime.max.replace(tzinfo=LOCAL_TZ)


def wallet_aliases(wallet_display: dict[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for key, display in wallet_display.items():
        for value in {key, display, display.replace(" ", ""), display.replace("_", " ")}:
            n = normalize(value)
            if n:
                aliases[n] = key
    return aliases


def resolve_wallet_queries(raw: str, wallet_display: dict[str, str], *, loose: bool = True) -> list[str]:
    aliases = wallet_aliases(wallet_display)
    query = normalize(raw)
    if not query:
        return []

    matches = []
    for alias, key in aliases.items():
        is_match = query == alias or (loose and (query in alias or alias in query))
        if is_match and key not in matches:
            matches.append(key)
    if matches:
        return matches

    close = difflib.get_close_matches(query, list(aliases), n=3, cutoff=0.72 if loose else 0.88)
    for alias in close:
        key = aliases[alias]
        if key not in matches:
            matches.append(key)
    return matches


def entry_search_text(slug: str, entry: dict[str, Any]) -> str:
    parts = [slug, entry.get("slug", ""), entry.get("name", ""), entry.get("link", ""), entry.get("tweet_author_handle", "")]
    return " ".join(str(p) for p in parts if p)


def find_slug_matches(data: dict[str, Any], query: str, limit: int = 8) -> list[tuple[str, dict[str, Any], float]]:
    q = normalize(query)
    if not q:
        return []
    scored = []
    for slug, entry in data.items():
        if not isinstance(entry, dict):
            continue
        text = entry_search_text(slug, entry)
        norm_fields = [normalize(slug), normalize(entry.get("slug", "")), normalize(entry.get("name", "")), normalize(entry.get("link", ""))]
        score = 0.0
        for field in norm_fields:
            if not field:
                continue
            if q == field:
                score = max(score, 1.0)
            elif q in field or field in q:
                score = max(score, 0.93)
            else:
                score = max(score, difflib.SequenceMatcher(None, q, field).ratio())
        if score >= 0.68:
            scored.append((slug, entry, score))
    scored.sort(key=lambda item: (item[2], -len(item[0])), reverse=True)
    return scored[:limit]


def split_csvish(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;|]", text or "") if part.strip()]


def parse_slug_args(args: str, wallet_display: dict[str, str]) -> tuple[list[str], list[str]]:
    wallet_part = ""
    slug_part = args.strip()
    m = re.search(r"(?:--wallet|-w|wallet:|akun:)\s+(.+)$", args, re.IGNORECASE)
    if m:
        wallet_part = m.group(1).strip()
        slug_part = args[: m.start()].strip()

    wallets = []
    if wallet_part:
        for item in split_csvish(wallet_part):
            wallets.extend(resolve_wallet_queries(item, wallet_display))
    else:
        # Also support: /slug veilsofcolors dxym wolfhead
        tokens = slug_part.split()
        trailing_wallets = []
        while tokens:
            candidate = " ".join(tokens[-2:]) if len(tokens) >= 2 else tokens[-1]
            resolved = resolve_wallet_queries(candidate, wallet_display, loose=False)
            if resolved:
                trailing_wallets = resolved + trailing_wallets
                tokens = tokens[:-2]
                continue
            resolved = resolve_wallet_queries(tokens[-1], wallet_display, loose=False)
            if resolved:
                trailing_wallets = resolved + trailing_wallets
                tokens = tokens[:-1]
                continue
            break
        if trailing_wallets:
            wallets = []
            for w in trailing_wallets:
                if w not in wallets:
                    wallets.append(w)
            slug_part = " ".join(tokens)

    slugs = split_csvish(slug_part) or ([slug_part] if slug_part else [])
    return slugs, wallets


def markdown_escape(text: str) -> str:
    # Existing pipeline uses Markdown v1, so escape only link-breaking chars where we add dynamic labels.
    return str(text).replace("[", "\\[").replace("]", "\\]").replace("`", "'")


def format_entry(
    slug: str,
    entry: dict[str, Any],
    wallet_filter: list[str] | None = None,
    today_only: bool = False,
    include_all_stages_when_today: bool = False,
) -> str:
    name = entry.get("name") or slug
    link = entry.get("link") or ""
    chain = entry.get("chain") or "Ethereum"
    source = entry.get("source") or "-"
    handle = entry.get("tweet_author_handle") or "-"
    wallet_display = load_wallet_display()

    title = f"*{markdown_escape(name)}* — {markdown_escape(chain)}"
    if link:
        title = f"[{markdown_escape(name)}]({link}) — {markdown_escape(chain)}"
    lines = [title, f"Slug: `{markdown_escape(slug)}` | Source: `{markdown_escape(source)}` | X: `{markdown_escape(handle)}`"]

    wallets = entry.get("wallets", {}) if isinstance(entry.get("wallets"), dict) else {}
    selected = wallet_filter or list(wallets.keys())
    any_wallet = False
    any_stage = False
    for wallet in selected:
        if wallet not in wallets:
            continue
        any_wallet = True
        display = wallet_display.get(wallet, wallet)
        stages = wallets.get(wallet, [])
        lines.append(f"*{markdown_escape(display)}:*")
        if isinstance(stages, list):
            has_today_stage = any(is_stage_today(s) for s in stages)
            if today_only and include_all_stages_when_today and has_today_stage:
                shown = stages
            else:
                shown = [s for s in stages if not today_only or is_stage_today(s)]
            if shown:
                any_stage = True
                lines.extend(str(s) for s in shown)
            else:
                lines.append("_No stage today._" if today_only else "_No stages listed._")
        else:
            text = str(stages)
            if not today_only or is_stage_today(text):
                any_stage = True
                lines.append(text)
            else:
                lines.append("_No stage today._")
    if wallet_filter and not any_wallet:
        lines.append("_Wallet filter did not match this entry._")
    if today_only and not any_stage:
        return ""
    return "\n".join(lines)


def filter_by_wallet(data: dict[str, Any], wallet_query: str, today_only: bool = False) -> tuple[str, list[str]]:
    wallet_display = load_wallet_display()
    wallets = resolve_wallet_queries(wallet_query, wallet_display)
    if not wallets:
        known = ", ".join(wallet_display.values() or wallet_display.keys())
        return f"Wallet/account tidak ketemu: `{markdown_escape(wallet_query)}`\nKnown: {markdown_escape(known)}", []

    results = []
    for slug, entry in sorted(data.items(), key=lambda item: extract_earliest_time(item[1]) if isinstance(item[1], dict) else datetime.max.replace(tzinfo=LOCAL_TZ)):
        if not isinstance(entry, dict):
            continue
        wallets_map = entry.get("wallets", {}) if isinstance(entry.get("wallets"), dict) else {}
        if not any(w in wallets_map for w in wallets):
            continue
        rendered = format_entry(
            slug,
            entry,
            wallet_filter=wallets,
            today_only=today_only,
            include_all_stages_when_today=today_only,
        )
        if rendered:
            results.append(rendered)
        if len(results) >= MAX_RESULTS:
            break

    label = "Mint Today" if today_only else "Upcoming Mints"
    wallet_names = ", ".join(wallet_display.get(w, w) for w in wallets)
    if not results:
        return f"*{label}* untuk `{markdown_escape(wallet_names)}`\n\nTidak ada entry yang cocok.", []
    more = "" if len(results) < MAX_RESULTS else f"\n\n_Dibatasi {MAX_RESULTS} entry. Pakai /slug untuk project spesifik._"
    return f"*{label}* untuk `{markdown_escape(wallet_names)}`\n\n" + "\n\n".join(results) + more, wallets


def filter_by_slug(data: dict[str, Any], args: str) -> str:
    wallet_display = load_wallet_display()
    queries, wallet_filter = parse_slug_args(args, wallet_display)
    if not queries:
        return usage("slug")

    rendered = []
    seen = set()
    not_found = []
    for query in queries:
        matches = find_slug_matches(data, query, limit=3)
        if not matches:
            not_found.append(query)
            continue
        # Show the best fuzzy hit by default to avoid huge typo queries exploding output.
        slug, entry, score = matches[0]
        if slug in seen:
            continue
        seen.add(slug)
        block = format_entry(slug, entry, wallet_filter=wallet_filter or None, today_only=False)
        if score < 0.9:
            block = f"_Fuzzy match for `{markdown_escape(query)}` (score {score:.2f})_\n" + block
        rendered.append(block)
        if len(rendered) >= MAX_RESULTS:
            break

    if not rendered:
        return "Slug/project tidak ketemu: " + ", ".join(f"`{markdown_escape(q)}`" for q in not_found)
    suffix = ""
    if not_found:
        suffix = "\n\nTidak ketemu: " + ", ".join(f"`{markdown_escape(q)}`" for q in not_found)
    return "*Slug Lookup*\n\n" + "\n\n".join(rendered) + suffix


def usage(command: str | None = None) -> str:
    text = (
        "*Upcoming Mints Commands*\n\n"
        "`/upcoming <wallet/account>`\n"
        "Contoh: `/upcoming DXYM 01`, `/upcoming Wolfhead`\n\n"
        "`/today <wallet/account>`\n"
        "Contoh: `/today DXYM 01`, `/today Wolfhead`\n\n"
        "`/slug <project/link/slug>[, project2] [--wallet <wallet/account>]`\n"
        "Contoh: `/slug veilsofcolors`, `/slug veilsofcolors --wallet DXYM 01`, `/slug veilsofcolors, neokitsune wolfhead`\n\n"
        "`/runcheck` atau `/runcheck status`\n"
        "Admin only: jalankan full NFT Mint Check pipeline."
    )
    return text


def api_call(method: str, payload: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> Any:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(body)
    return body.get("result")


def split_message(text: str, max_len: int = 3900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        pos = rest.rfind("\n\n", 0, max_len)
        if pos < max_len // 3:
            pos = rest.rfind("\n", 0, max_len)
        if pos < max_len // 3:
            pos = max_len
        chunks.append(rest[:pos])
        rest = rest[pos:].lstrip()
    return chunks


def send_message(chat_id: int | str, text: str, thread_id: int | None = None) -> None:
    for chunk in split_message(text):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        api_call("sendMessage", payload=payload)
        time.sleep(0.3)


def allowed_chat(chat_id: int | str) -> bool:
    if not TELEGRAM_CHAT_ID:
        return True
    return str(chat_id) == str(TELEGRAM_CHAT_ID)


def allowed_thread(thread_id: int | str | None) -> bool:
    if not UPCOMING_MINTS_ALLOWED_THREAD_IDS:
        return True
    allowed = {item.strip() for item in UPCOMING_MINTS_ALLOWED_THREAD_IDS.split(",") if item.strip()}
    return str(thread_id or "") in allowed


def parse_command(text: str) -> tuple[str, str] | None:
    if not text or not text.startswith("/"):
        return None
    first, _, rest = text.partition(" ")
    cmd = first.split("@", 1)[0].lower().lstrip("/")
    return cmd, rest.strip()


def is_admin_user(user_id: int | str | None) -> bool:
    if not ADMIN_USER_IDS:
        return False
    return str(user_id or "") in ADMIN_USER_IDS


def pipeline_is_running() -> bool:
    with PIPELINE_LOCK:
        return PIPELINE_RUNNING


def set_pipeline_running(value: bool) -> None:
    global PIPELINE_RUNNING
    with PIPELINE_LOCK:
        PIPELINE_RUNNING = value


def tail_text(text: str, limit: int = 900) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def run_pipeline_worker(chat_id: int | str, thread_id: int | None) -> None:
    started = time.time()
    try:
        cmd = shlex.split(PIPELINE_COMMAND)
        if not cmd:
            raise RuntimeError("UPCOMING_PIPELINE_COMMAND kosong")
        PIPELINE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log(f"pipeline started: {PIPELINE_COMMAND}")
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=PIPELINE_TIMEOUT,
        )
        elapsed = int(time.time() - started)
        output = "\n".join(part for part in [result.stdout, result.stderr] if part)
        with open(PIPELINE_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== manual run {datetime.now(LOCAL_TZ).isoformat(timespec='seconds')} =====\n")
            fh.write(output)
            fh.write(f"\nexit_code={result.returncode} elapsed={elapsed}s\n")
        if result.returncode == 0:
            send_message(chat_id, f"✅ NFT Mint Check selesai dalam {elapsed}s. Laporan sudah dikirim ke topic Upcoming Mints.", thread_id=thread_id)
            log(f"pipeline done exit=0 elapsed={elapsed}s")
        else:
            send_message(chat_id, f"❌ NFT Mint Check gagal (exit {result.returncode}) setelah {elapsed}s.\n\n```\n{tail_text(output)}\n```", thread_id=thread_id)
            log(f"pipeline failed exit={result.returncode} elapsed={elapsed}s")
    except subprocess.TimeoutExpired as exc:
        elapsed = int(time.time() - started)
        raw_output = "\n".join(str(part) for part in [exc.stdout or "", exc.stderr or ""] if part)
        send_message(chat_id, f"⏱️ NFT Mint Check timeout setelah {elapsed}s.\n\n```\n{tail_text(raw_output)}\n```", thread_id=thread_id)
        log(f"pipeline timeout elapsed={elapsed}s")
    except Exception as exc:
        send_message(chat_id, f"❌ Gagal menjalankan NFT Mint Check: `{exc}`", thread_id=thread_id)
        log(f"pipeline exception: {exc}")
    finally:
        set_pipeline_running(False)


def start_pipeline(chat_id: int | str, thread_id: int | None) -> bool:
    with PIPELINE_LOCK:
        global PIPELINE_RUNNING
        if PIPELINE_RUNNING:
            return False
        PIPELINE_RUNNING = True
    thread = threading.Thread(target=run_pipeline_worker, args=(chat_id, thread_id), daemon=True)
    thread.start()
    return True


def handle_command(text: str, *, user_id: int | str | None = None, chat_id: int | str | None = None, thread_id: int | None = None) -> str | None:
    parsed = parse_command(text)
    if not parsed:
        return None
    cmd, args = parsed
    if cmd not in {"start", "help", "upcoming", "today", "slug", "runcheck"}:
        return None
    if cmd in {"start", "help"}:
        return usage()
    if cmd == "runcheck":
        if not is_admin_user(user_id):
            return "Command ini hanya untuk admin yang di-allowlist."
        if chat_id is None:
            return "Tidak bisa menjalankan pipeline: chat_id tidak ditemukan."
        if args and args.lower() == "status":
            status = "sedang running" if pipeline_is_running() else "idle"
            return f"NFT Mint Check pipeline: {status}."
        if not start_pipeline(chat_id, thread_id):
            return "NFT Mint Check pipeline masih running. Tunggu selesai dulu."
        return "🚀 NFT Mint Check full pipeline dimulai. Aku kabarin kalau selesai/gagal."

    data = load_json(UPCOMING_FILE, {})
    if not isinstance(data, dict) or not data:
        return "upcoming_mints.json kosong / belum ada data."

    if cmd == "upcoming":
        if not args:
            return usage("upcoming")
        return filter_by_wallet(data, args, today_only=False)[0]
    if cmd == "today":
        if not args:
            return usage("today")
        return filter_by_wallet(data, args, today_only=True)[0]
    if cmd == "slug":
        return filter_by_slug(data, args)
    return None


def get_initial_offset() -> int:
    state = load_json(STATE_FILE, {})
    if isinstance(state, dict) and isinstance(state.get("offset"), int):
        return state["offset"]
    return 0


def run() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    me = api_call("getMe")
    log(f"started as @{me.get('username', 'unknown')}")
    offset = get_initial_offset()
    while True:
        try:
            updates = api_call("getUpdates", params={"timeout": POLL_TIMEOUT, "offset": offset, "allowed_updates": json.dumps(["message"])})
            for update in updates:
                offset = int(update["update_id"]) + 1
                save_json(STATE_FILE, {"offset": offset})
                msg = update.get("message") or {}
                text = msg.get("text") or ""
                chat = msg.get("chat") or {}
                sender = msg.get("from") or {}
                user_id = sender.get("id")
                chat_id = chat.get("id")
                thread_id = msg.get("message_thread_id")
                if chat_id is None or not allowed_chat(chat_id):
                    continue
                if parse_command(text) and not allowed_thread(thread_id):
                    log(f"ignored command outside allowed thread: {text.split()[0]} chat={chat_id} thread={thread_id}")
                    continue
                response = handle_command(text, user_id=user_id, chat_id=chat_id, thread_id=thread_id)
                if response:
                    send_message(chat_id, response, thread_id=thread_id)
                    log(f"handled {text.split()[0]} chat={chat_id} thread={thread_id} user={user_id}")
        except urllib.error.HTTPError as exc:
            log(f"telegram HTTP error: {exc.code} {exc.read()[:300]!r}")
            time.sleep(5)
        except Exception as exc:
            log(f"loop error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    run()
