#!/usr/bin/env python3
"""
NFT Mint Cron Pipeline — fully deterministic.
Steps:
1. Scrape X Lists → extract mint links
2. Cleanup expired entries (1h after last tier)
3. Check eligibility for new slugs via browser
4. Parse & update upcoming_mints.json
5. Send Telegram reports: Upcoming Mints + Mint Today

Run via: python3 cron_pipeline.py
Requires: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID env vars
"""
import json, os, re, signal, subprocess, sys, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

WORKLOAD_ROOT = Path(__file__).resolve().parents[2]  # workload root
BASE_DIR = WORKLOAD_ROOT  # alias kept for downstream usages
# Auto-detect local timezone from system
def _detect_local_tz():
    # 1. Check TZ environment variable first
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_env)
        except Exception:
            pass
    # 2. Try system local timezone
    try:
        offset = datetime.now(timezone.utc).astimezone().utcoffset()
        if offset and offset.total_seconds() != 0:
            return timezone(offset)
    except Exception:
        pass
    return timezone(timedelta(hours=7))  # fallback

LOCAL_TZ = _detect_local_tz()
LOCAL_TZ_OFFSET = LOCAL_TZ.utcoffset(datetime.now())
LOCAL_TZ_NAME = f"GMT+{int(LOCAL_TZ_OFFSET.total_seconds() // 3600)}" if LOCAL_TZ_OFFSET.total_seconds() >= 0 else f"GMT{int(LOCAL_TZ_OFFSET.total_seconds() // 3600)}"

CONFIG_DIR = BASE_DIR / "config"
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
UPCOMING_FILE = STATE_DIR / "upcoming_mints.json"
WALLETS_FILE = CONFIG_DIR / "wallets.json"
# WALLET_DISPLAY loaded dynamically from wallets.json in main()
WALLET_DISPLAY = {}

def load_wallet_display():
    """Load wallet display names from wallets.json."""
    global WALLET_DISPLAY
    data = load_json(WALLETS_FILE)
    if isinstance(data, dict):
        # New format: {"wallet_key": {"display": "Name"}, ...}
        WALLET_DISPLAY = {k: v.get("display", k) for k, v in data.items()}
    elif isinstance(data, list):
        # Old format: ["wallet_1", "wallet_2"] — use keys as display
        WALLET_DISPLAY = {k: k for k in data}
OPENSEA_CHECKER_DIR = WORKLOAD_ROOT / "scripts" / "checker" / "opensea_checker"
ELIG_SCRIPT = str(OPENSEA_CHECKER_DIR / "opensea_checker_api_batch.py")
# Browser-based fallback when the API checker fails/times out. Defaults to the
# real opensea_checker_browser_batch.py shipped with this workload — previously
# this defaulted to "/path/to/check_eligibility_batch.py" which would silently
# FileNotFoundError on fallback, so any API outage would skip eligibility
# entirely. Override via env if your browser checker lives outside this tree.
_DEFAULT_FALLBACK_SCRIPT = OPENSEA_CHECKER_DIR / "opensea_checker_browser_batch.py"
ELIG_FALLBACK_SCRIPT = os.environ.get("NFT_TRADE_ELIG_FALLBACK", str(_DEFAULT_FALLBACK_SCRIPT))
CUSTOM_ELIG_SCRIPT = str(WORKLOAD_ROOT / "scripts" / "checker" / "custom_site_checker.py")
SCRAPER_SCRIPT = str(WORKLOAD_ROOT / "scripts" / "scraper" / "x_list_scraper.py")
COOKIES_FILE = os.environ.get("TWITTER_COOKIES_FILE", "/path/to/twitter_cookies.json")
OPENSEA_API_ERROR_LOG = LOG_DIR / "opensea_api_errors.log"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_MESSAGE_THREAD_ID = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")

# Runtime budget guard so eligibility step can't consume the whole cron window.
PIPELINE_START_TS = None
PIPELINE_MAX_SECONDS = int(os.environ.get("NFT_PIPELINE_MAX_SECONDS", "540"))
MIN_SEND_BUFFER_SECONDS = int(os.environ.get("NFT_PIPELINE_SEND_BUFFER_SECONDS", "45"))

LOG = []  # collect log lines

def log(msg):
    print(msg)
    LOG.append(msg)


def elapsed_seconds():
    if PIPELINE_START_TS is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - PIPELINE_START_TS).total_seconds()))


def remaining_budget_seconds(reserve_send_buffer=True):
    reserve = MIN_SEND_BUFFER_SECONDS if reserve_send_buffer else 0
    remaining = PIPELINE_MAX_SECONDS - elapsed_seconds() - reserve
    return max(0, remaining)

# ── helpers ────────────────────────────────────────────────

def load_json(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def parse_tz_str(time_str):
    if not time_str:
        return None
    try:
        now = datetime.now(LOCAL_TZ)
        gmt_match = re.search(r"GMT([+-]\d+)", time_str)
        if gmt_match:
            clean = re.sub(r"GMT[+-]\d+", "", time_str).strip()
            clean = clean.replace(" at ", " ")
            for fmt in ("%d %b %H:%M %Y", "%B %d %I:%M %p %Y"):
                try:
                    dt = datetime.strptime(f"{clean} {now.year}", fmt)
                    dt = dt.replace(tzinfo=LOCAL_TZ)
                    if dt < now - timedelta(days=30):
                        dt = dt.replace(year=now.year + 1)
                    return dt
                except ValueError:
                    pass
            return None
        if "UTC" in time_str:
            clean = time_str.replace("UTC", "").replace("at", "").strip()
            dt = datetime.strptime(f"{clean} {now.year}", "%B %d %I:%M %p %Y")
            dt = dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            if dt < now - timedelta(days=30):
                dt = dt.replace(year=now.year + 1)
            return dt
        dt = datetime.strptime(f"{time_str} {now.year}", "%d %b %H:%M %Y")
        dt = dt.replace(tzinfo=LOCAL_TZ)
        if dt < now - timedelta(days=30):
            dt = dt.replace(year=now.year + 1)
        return dt
    except (ValueError, TypeError):
        return None

def convert_utc_to_local_in_line(line):
    if re.search(r"GMT[+-]\d+", line):
        return line
    m = re.search(r"(Started|Ended):\s*(.+?)(\s+UTC)?$", line, re.IGNORECASE)
    if m:
        prefix = m.group(1)
        time_str = m.group(2).strip()
        dt = parse_tz_str(time_str + " UTC")
        if dt:
            formatted = dt.strftime(f"%d %b %H:%M {LOCAL_TZ_NAME}")
            return line[:m.start()] + f"{prefix}: {formatted}"
    return line

# ── eligibility check ─────────────────────────────────────

def run_eligibility_check(slugs):
    wallets = list(WALLET_DISPLAY.keys())
    if not slugs:
        log("[*] No slugs to check")
        return {}
    budget = remaining_budget_seconds(reserve_send_buffer=True)
    if budget < 90:
        log(f"[!] Skipping OpenSea eligibility check, remaining budget too low: {budget}s")
        return {}
    timeout_s = min(420, budget)
    wallet_str = ",".join(wallets)
    slug_str = ",".join(slugs)
    log(f"[*] Checking eligibility for {len(slugs)} projects: {slug_str}")
    log(f"[*] OpenSea eligibility timeout budget: {timeout_s}s (elapsed={elapsed_seconds()}s, max={PIPELINE_MAX_SECONDS}s)")
    log("[*] Using OpenSea API eligibility checker (SIWE + GraphQL, no browser)")

    cmd = ["python3", ELIG_SCRIPT, wallet_str, slug_str]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout_s,
            cwd=str(OPENSEA_CHECKER_DIR)
        )
    except subprocess.TimeoutExpired:
        log(f"[!] OpenSea API eligibility timed out after {timeout_s}s")
        result = None

    if result is None or result.returncode != 0:
        err_tail = ((result.stderr or result.stdout or "") if result else "timeout")[-1200:]
        log(f"[!] OpenSea API eligibility failed: {err_tail}")
        with open(OPENSEA_API_ERROR_LOG, "a") as f:
            f.write(f"\n[{datetime.now(timezone.utc).isoformat()}] API checker failed for {slug_str}:\n{err_tail}\n")

        fallback_budget = remaining_budget_seconds(reserve_send_buffer=True)
        if fallback_budget < 120:
            log(f"[!] Skipping browser fallback, remaining budget too low: {fallback_budget}s")
            return {}
        fallback_timeout = min(420, fallback_budget)
        fallback_cmd = ["python3", ELIG_FALLBACK_SCRIPT, wallet_str, slug_str]
        if not os.environ.get("DISPLAY"):
            fallback_cmd = ["xvfb-run", "-a"] + fallback_cmd
            log("[*] No DISPLAY detected, running browser fallback via xvfb-run")
        log(f"[*] Falling back to browser OpenSea checker, timeout: {fallback_timeout}s")
        try:
            result = subprocess.run(
                fallback_cmd,
                capture_output=True, text=True, timeout=fallback_timeout,
                cwd=str(Path(ELIG_FALLBACK_SCRIPT).parent)
            )
        except subprocess.TimeoutExpired:
            log(f"[!] Browser fallback timed out after {fallback_timeout}s")
            return {}
        if result.returncode != 0:
            err_tail = (result.stderr or result.stdout or "")[-1200:]
            log(f"[!] Browser fallback failed: {err_tail}")
            return {}

    output = result.stdout
    report_start = output.find("FINAL REPORT")
    if report_start == -1:
        log("[!] No FINAL REPORT found in output")
        with open(OPENSEA_API_ERROR_LOG, "a") as f:
            f.write(f"\n[{datetime.now(timezone.utc).isoformat()}] No FINAL REPORT for {slug_str}:\n{output[-1200:]}\n")
        return {}
    report_text = output[report_start:]
    with open(STATE_DIR / "last_elig_report.txt", 'w') as f:
        f.write(report_text)
    log(f"[+] Eligibility check done, report saved")
    return report_text


def run_custom_eligibility_check(project_keys):
    if not project_keys:
        return {}
    budget = remaining_budget_seconds(reserve_send_buffer=True)
    if budget < 60:
        log(f"[!] Skipping custom-site eligibility check, remaining budget too low: {budget}s")
        return {}
    timeout_s = min(180, budget)
    key_str = ",".join(project_keys)
    log(f"[*] Checking custom-site eligibility for {len(project_keys)} projects: {key_str}")
    log(f"[*] Custom-site eligibility timeout budget: {timeout_s}s (elapsed={elapsed_seconds()}s, max={PIPELINE_MAX_SECONDS}s)")
    cmd = ["python3", CUSTOM_ELIG_SCRIPT, key_str]
    if not os.environ.get("DISPLAY"):
        cmd = ["xvfb-run", "-a"] + cmd
        log("[*] No DISPLAY detected, running custom eligibility via xvfb-run")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout_s,
            cwd=str(BASE_DIR)
        )
    except subprocess.TimeoutExpired:
        log(f"[!] Custom eligibility check timed out after {timeout_s}s")
        return {}
    if result.returncode != 0:
        err_tail = (result.stderr or result.stdout or "")[-1200:]
        log(f"[!] Custom eligibility check failed: {err_tail}")
        return {}
    try:
        parsed = json.loads(result.stdout.strip() or "{}")
        log(f"[+] Custom eligibility check done, parsed {len(parsed)} projects")
        return parsed
    except Exception as e:
        log(f"[!] Failed parsing custom eligibility JSON: {e}")
        tail = result.stdout[-1200:] if result.stdout else ""
        if tail:
            log(tail)
        return {}

# ── parse eligibility report ───────────────────────────────

def parse_eligibility_report_to_upcoming(report_text):
    with open(WALLETS_FILE) as f:
        wallets = json.load(f)
    lines = report_text.split('\n')
    results = {}
    current_project = None
    current_slug = None
    current_wallet = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line[0].isdigit() and '. ' in line:
            if current_slug and current_project:
                results[current_slug] = current_project
            m = re.match(r'\d+\.\s*\[([^\]]+)\]\(([^)]+)\)\s*[—\-]\s*(.+)', line)
            if m:
                link = m.group(2)
                slug = link.split('/collection/')[-1].split('/')[0] if '/collection/' in link else m.group(1)
                current_project = {
                    'name': m.group(1),
                    'link': link,
                    'chain': m.group(3).strip(),
                    'wallets': {},
                    'source': 'opensea',
                    'tweet_author_handle': '',
                }
                current_slug = slug
            else:
                parts = line.split(' - ')
                if len(parts) >= 3:
                    name = parts[0].split('. ', 1)[1] if '. ' in parts[0] else parts[0]
                    link = parts[2].strip()
                    slug = link.split('/collection/')[-1].split('/')[0] if '/collection/' in link else name
                    current_project = {
                        'name': name,
                        'link': link,
                        'chain': parts[1].strip(),
                        'wallets': {},
                        'source': 'opensea',
                        'tweet_author_handle': '',
                    }
                    current_slug = slug
            current_wallet = None
            continue
        if not current_project:
            continue
        wallet_match = re.match(r'\*?\*?([^*:]+)\*?\*?:\s*$', line.replace('*', ''))
        if wallet_match:
            wallet_label = wallet_match.group(1).strip()
            for w in wallets:
                display = WALLET_DISPLAY.get(w, w)
                if display.lower().replace(' ', '') in wallet_label.lower().replace(' ', '').replace('-', '') or \
                   w.lower().replace(' ', '') in wallet_label.lower().replace(' ', '').replace('-', ''):
                    current_wallet = w
                    current_project['wallets'][current_wallet] = []
                    break
            continue
        if current_wallet and line[:1] in '✅❌❔':
            converted = convert_utc_to_local_in_line(line)
            current_project['wallets'][current_wallet].append(converted)

    if current_slug and current_project:
        results[current_slug] = current_project
    return results


def normalize_stage_line(stage):
    if not stage:
        return '❌ TBA (TBA, limit TBA) — TBA'
    line = stage.strip()
    if '—' not in line:
        icon = '✅' if line.startswith('✅') else '❌'
        if '(' not in line:
            body = line[1:].strip() if line[:1] in '✅❌' else line
            return f"{icon} {body} (TBA, limit TBA) — TBA"
        return f"{line} — TBA"
    return line


def merge_project_entry(existing, incoming):
    if not existing:
        merged = incoming.copy()
        if incoming.get('source') == 'custom_site':
            merged['wallets'] = {k: [str(s).strip() or 'TBA' for s in v] for k, v in incoming.get('wallets', {}).items()}
        else:
            merged['wallets'] = {k: [normalize_stage_line(s) for s in v] for k, v in incoming.get('wallets', {}).items()}
        return merged

    merged = dict(existing)
    incoming_source = incoming.get('source') or existing.get('source')
    existing_source = existing.get('source')

    # Priority rule: OpenSea data wins over custom-site scrape.
    # If existing is OpenSea and incoming is custom-site, keep OpenSea data
    # (custom-site scrape may have failed → "Not Found" / handle-based name must not
    # overwrite valid OpenSea name/link/wallets)
    if existing_source == 'opensea' and incoming_source == 'custom_site':
        merged['name'] = existing.get('name') or incoming.get('name')
        merged['link'] = existing.get('link') or incoming.get('link')
        merged['chain'] = existing.get('chain') or incoming.get('chain') or 'TBA'
        merged['source'] = incoming_source
        if incoming.get('last_seen'):
            merged['last_seen'] = incoming.get('last_seen')
        if incoming.get('tweet_author_handle'):
            merged['tweet_author_handle'] = incoming.get('tweet_author_handle')
        elif existing.get('tweet_author_handle'):
            merged['tweet_author_handle'] = existing.get('tweet_author_handle')
        # Keep existing OpenSea wallets untouched
        return merged

    merged['name'] = incoming.get('name') or existing.get('name')
    merged['link'] = incoming.get('link') or existing.get('link')
    merged['chain'] = incoming.get('chain') or existing.get('chain') or 'TBA'
    merged['source'] = incoming_source
    if incoming.get('last_seen'):
        merged['last_seen'] = incoming.get('last_seen')
    if incoming.get('tweet_author_handle'):
        merged['tweet_author_handle'] = incoming.get('tweet_author_handle')
    elif existing.get('tweet_author_handle'):
        merged['tweet_author_handle'] = existing.get('tweet_author_handle')

    existing_wallets = dict(existing.get('wallets', {}))
    incoming_wallets = incoming.get('wallets', {})
    for wallet, stages in incoming_wallets.items():
        if incoming_source == 'custom_site':
            norm = [str(s).strip() or 'TBA' for s in stages] if isinstance(stages, list) else [str(stages).strip() or 'TBA']
        else:
            norm = [normalize_stage_line(s) for s in stages] if isinstance(stages, list) else [normalize_stage_line(str(stages))]
        existing_wallets[wallet] = norm
    merged['wallets'] = existing_wallets
    return merged

# ── cleanup ────────────────────────────────────────────────

def cleanup_expired_upcoming(data):
    now = datetime.now(LOCAL_TZ)
    cleaned = {}
    removed = []
    for key, entry in data.items():
        source = entry.get("source", "opensea")
        if source == "custom_site":
            last_seen = entry.get("last_seen")
            if last_seen:
                try:
                    last_seen_dt = datetime.fromisoformat(last_seen)
                    if last_seen_dt.tzinfo is None:
                        last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
                    last_seen_dt = last_seen_dt.astimezone(LOCAL_TZ)
                    if last_seen_dt + timedelta(days=1) < now:
                        removed.append(key)
                        continue
                except Exception:
                    pass
            cleaned[key] = entry
            continue

        # Scan ALL stages from ALL wallets, pick the LATEST datetime found.
        # Stages may carry the time either after a "—" separator (normal case)
        # or inside a "Started:/Ended:" prefix (legacy/edge case).
        latest_dt = None
        time_pat = re.compile(
            r"(\d{1,2}\s+\w+\s+\d{2}:\d{2}\s*GMT\+\d+|\w+\s+\d{1,2}\s+at\s+\d{1,2}:\d{2}\s+[AP]M\s*GMT\+\d+)",
            re.IGNORECASE,
        )
        for wallet, stages in entry.get("wallets", {}).items():
            if not isinstance(stages, list):
                continue
            for stage in stages:
                if not isinstance(stage, str):
                    continue
                for match in time_pat.finditer(stage):
                    dt = parse_tz_str(match.group(1).strip())
                    if dt and (latest_dt is None or dt > latest_dt):
                        latest_dt = dt
        if latest_dt is None:
            # OpenSea entries with no tier time usually mean the mint page is gone,
            # ended, or metadata was unavailable. Fall back to discovery/check time.
            fallback_seen = entry.get("last_seen") or entry.get("last_check")
            if fallback_seen:
                try:
                    fallback_dt = datetime.fromisoformat(str(fallback_seen))
                    if fallback_dt.tzinfo is None:
                        fallback_dt = fallback_dt.replace(tzinfo=timezone.utc)
                    fallback_dt = fallback_dt.astimezone(LOCAL_TZ)
                    if fallback_dt + timedelta(days=1) < now:
                        removed.append(key)
                        continue
                except Exception:
                    pass
            elif not any(entry.get("wallets", {}).values()):
                removed.append(key)
                continue
            cleaned[key] = entry
            continue
        if latest_dt + timedelta(hours=1) < now:
            removed.append(key)
            continue
        cleaned[key] = entry
    if removed:
        log(f"[*] Cleanup: removed {len(removed)} expired: {', '.join(removed)}")
    return cleaned

# ── format reports ─────────────────────────────────────────

def extract_earliest_time(entry):
    earliest = None
    for wallet, stages in entry.get("wallets", {}).items():
        if isinstance(stages, list):
            for s in stages:
                m = re.search(r"—\s*(\d{1,2}\s+\w+\s+\d{2}:\d{2})\s+GMT\+7", s)
                if m:
                    dt = parse_tz_str(m.group(1) + " " + LOCAL_TZ_NAME)
                    if dt and (earliest is None or dt < earliest):
                        earliest = dt
    return earliest or datetime.max.replace(tzinfo=LOCAL_TZ)

def is_stage_today(stage_str):
    now = datetime.now(LOCAL_TZ)
    m = re.search(r"—\s*(\d{1,2})\s+(\w+)\s+\d{2}:\d{2}\s+GMT\+7", stage_str)
    if not m:
        return False
    try:
        month = datetime.strptime(m.group(2), "%b").month
    except ValueError:
        return False
    return int(m.group(1)) == now.day and month == now.month

def format_upcoming_report(upcoming):
    if not upcoming:
        return ""
    sorted_upcoming = sorted(upcoming.items(), key=lambda x: extract_earliest_time(x[1]))
    lines = ["*📋 Upcoming Mints*\n"]
    for idx, (slug, entry) in enumerate(sorted_upcoming, start=1):
        name = entry.get("name", slug)
        link = entry.get("link", "")
        chain = entry.get("chain", "Ethereum")
        lines.append(f"{idx}. [{name}]({link}) — {chain}")
        for wallet, stages in entry.get("wallets", {}).items():
            display = WALLET_DISPLAY.get(wallet, wallet)
            lines.append(f"*{display}:*")
            if isinstance(stages, list):
                for stage in stages:
                    lines.append(stage)
            else:
                lines.append(str(stages))
        lines.append("")
    return "\n".join(lines)

def format_today_report(upcoming):
    if not upcoming:
        return ""
    today_entries = {}
    for slug, entry in upcoming.items():
        has_today = False
        today_wallets = {}
        for wallet, stages in entry.get("wallets", {}).items():
            if not isinstance(stages, list):
                continue
            today_wallets[wallet] = stages
            for s in stages:
                if is_stage_today(s):
                    has_today = True
        if has_today and today_wallets:
            today_entries[slug] = {
                "name": entry.get("name", slug),
                "link": entry.get("link", ""),
                "chain": entry.get("chain", "Ethereum"),
                "wallets": today_wallets,
            }
    if not today_entries:
        return ""
    sorted_today = sorted(today_entries.items(), key=lambda x: extract_earliest_time(x[1]))
    now = datetime.now(LOCAL_TZ)
    lines = [f"*📅 Mint Today ({now.strftime('%d %b')})*\n"]
    for idx, (slug, entry) in enumerate(sorted_today, start=1):
        name = entry.get("name", slug)
        link = entry.get("link", "")
        chain = entry.get("chain", "Ethereum")
        lines.append(f"{idx}. [{name}]({link}) — {chain}")
        for wallet, stages in entry.get("wallets", {}).items():
            display = WALLET_DISPLAY.get(wallet, wallet)
            lines.append(f"*{display}:*")
            for stage in stages:
                lines.append(stage)
        lines.append("")
    return "\n".join(lines)

# ── Telegram send (sync) ──────────────────────────────────

def _send_single_telegram(text):
    """Send a single Telegram message (must be <= 4096 chars)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload_data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if TELEGRAM_MESSAGE_THREAD_ID:
        payload_data["message_thread_id"] = int(TELEGRAM_MESSAGE_THREAD_ID)
    payload = json.dumps(payload_data).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True
    except Exception as e:
        log(f"[!] Telegram send failed: {e}")
        return False


def _split_telegram_message(text, max_len=4000):
    """Split long text into chunks, breaking at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try to split at last double-newline (paragraph break) within limit
        split_pos = remaining.rfind('\n\n', 0, max_len)
        if split_pos < max_len // 2:
            # Fallback: split at last single newline
            split_pos = remaining.rfind('\n', 0, max_len)
        if split_pos < max_len // 3:
            # Last resort: hard split
            split_pos = max_len
        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:].lstrip('\n')
    return chunks


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("[!] Telegram not configured, skipping")
        return False
    if not text:
        return False
    chunks = _split_telegram_message(text)
    log(f"[*] Sending Telegram: {len(chunks)} message(s)")
    ok = True
    for i, chunk in enumerate(chunks):
        if not _send_single_telegram(chunk):
            ok = False
        if i < len(chunks) - 1:
            import time; time.sleep(1)  # rate limit guard
    if ok:
        log("[+] Telegram messages sent")
    return ok

# ── main ──────────────────────────────────────────────────

def main():
    global PIPELINE_START_TS
    PIPELINE_START_TS = datetime.now(timezone.utc)
    log("=" * 60)
    log("NFT Mint Cron Pipeline")
    log(f"Time: {datetime.now(LOCAL_TZ).strftime('%d %b %Y %H:%M')} {LOCAL_TZ_NAME}")
    log("=" * 60)

    # Check cookies exist
    if not os.path.exists(COOKIES_FILE):
        log("[!] Twitter cookies not found, scraper may fail")
    else:
        log("[*] Twitter cookies OK")

    load_wallet_display()
    log(f"[*] Wallets: {list(WALLET_DISPLAY.keys())}")
    with open(WALLETS_FILE) as f:
        wallets_data = json.load(f)
    # Support both formats: list or dict
    wallets = list(wallets_data.keys()) if isinstance(wallets_data, dict) else wallets_data

    # ── Step 1: Scrape X Lists ──
    log("\n[Step 1/5] Scraping X Lists...")
    scraper_proc = None
    try:
        # start_new_session=True puts the scraper in its own process group so that,
        # on timeout, we can kill the WHOLE tree (python + chromium/node children).
        # Killing only the python parent leaves chromium holding the stdout pipe,
        # which makes communicate() hang forever and swallows the timeout message.
        scraper_proc = subprocess.Popen(
            ["python3", SCRAPER_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = scraper_proc.communicate(timeout=420)
        except subprocess.TimeoutExpired:
            log("[!] Scraper timed out after 420s, killing process group")
            try:
                os.killpg(os.getpgid(scraper_proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = scraper_proc.communicate()
            stdout, stderr = stdout or "", stderr or ""
        else:
            err = stderr[-300:] if stderr else ""
            if scraper_proc.returncode == 0:
                log("[+] Scraper completed")
            else:
                log(f"[!] Scraper exited with code {scraper_proc.returncode}")
                if err:
                    log(f"[!] Scraper stderr: {err}")
    except Exception as e:
        log(f"[!] Scraper error: {e}")
        if scraper_proc and scraper_proc.poll() is None:
            try:
                os.killpg(os.getpgid(scraper_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # ── Load scraped links ──
    scraped_file = STATE_DIR / "scraped_links.json"
    slugs = []
    custom_project_keys = []
    scraped_project_links = {}
    handle_to_existing_key = {}
    if scraped_file.exists():
        with open(scraped_file) as f:
            scraped = json.load(f)
        slugs = list(dict.fromkeys(scraped.get("opensea_slugs", [])))
        scraped_project_links = scraped.get("project_links", {})
        custom_project_keys = [
            key for key, value in scraped_project_links.items()
            if value.get("custom_checker_links") and not value.get("opensea_link")
        ]
        log(f"[*] Found {len(slugs)} unique slugs from X Lists: {slugs}")
        log(f"[*] Found {len(custom_project_keys)} custom-site checker projects from X Lists: {custom_project_keys}")
    else:
        log("[*] No scraped_links.json found, skipping scrape step")

    # ── Load existing data ──
    upcoming = load_json(UPCOMING_FILE)
    for key, entry in upcoming.items():
        handle = (entry.get("tweet_author_handle") or '').lower().strip()
        if handle:
            handle_to_existing_key[handle] = key
    log(f"[*] Existing upcoming entries: {len(upcoming)}")

    # ── Step 2: Cleanup expired ──
    log("\n[Step 2/5] Cleaning up expired entries (last tier +1h)...")
    upcoming = cleanup_expired_upcoming(upcoming)
    log(f"[*] After cleanup: {len(upcoming)} entries")

    # ── Step 3: Eligibility check for new slugs ──
    if slugs or custom_project_keys:
        log(f"\n[Step 3/5] Checking eligibility...")
        if slugs:
            report_text = run_eligibility_check(slugs)
            if report_text:
                log("\n[Step 4/5] Updating upcoming_mints.json...")
                new_upcoming = parse_eligibility_report_to_upcoming(report_text)
                log(f"[*] Parsed {len(new_upcoming)} OpenSea projects from eligibility report")
                for slug, data in new_upcoming.items():
                    scraped_meta = scraped_project_links.get(slug, {})
                    handle = scraped_meta.get('tweet_author_handle', '')
                    data['tweet_author_handle'] = handle
                    merge_key = slug
                    existing = upcoming.get(merge_key)
                    if not existing and handle:
                        existing_key = handle_to_existing_key.get(handle.lower().strip())
                        if existing_key:
                            merge_key = existing_key
                            existing = upcoming.get(existing_key)
                    upcoming[merge_key] = merge_project_entry(existing, data)
                    if merge_key != slug and slug in upcoming:
                        del upcoming[slug]
                    if handle:
                        handle_to_existing_key[handle.lower().strip()] = merge_key
                    log(f"  + {data['name']} ({merge_key}) [opensea]")

        if custom_project_keys:
            custom_results = run_custom_eligibility_check(custom_project_keys)
            if custom_results:
                if not slugs:
                    log("\n[Step 4/5] Updating upcoming_mints.json...")
                log(f"[*] Parsed {len(custom_results)} custom-site projects")
                for key, data in custom_results.items():
                    scraped_meta = scraped_project_links.get(key, {})
                    handle = scraped_meta.get('tweet_author_handle', '')
                    data['tweet_author_handle'] = handle
                    # Override auto-generated page names with tweet handle
                    if handle:
                        raw_name = (data.get('name') or '').strip()
                        if not raw_name or raw_name in ('Unknown Project', '← BACK', 'Sign In', 'Check') or len(raw_name) > 60:
                            data['name'] = handle
                    merge_key = key
                    existing = upcoming.get(merge_key)
                    if not existing and handle:
                        existing_key = handle_to_existing_key.get(handle.lower().strip())
                        if existing_key:
                            merge_key = existing_key
                            existing = upcoming.get(existing_key)
                    if scraped_meta.get('primary_link') and not data.get('link'):
                        data['link'] = scraped_meta.get('primary_link')
                    if scraped_meta.get('opensea_link') and not data.get('link'):
                        data['link'] = scraped_meta.get('opensea_link')
                    data['last_seen'] = datetime.now(timezone.utc).isoformat()
                    upcoming[merge_key] = merge_project_entry(existing, data)
                    if handle:
                        handle_to_existing_key[handle.lower().strip()] = merge_key
                    log(f"  + {data.get('name', merge_key)} ({merge_key}) [custom]")

        upcoming = cleanup_expired_upcoming(upcoming)
        save_json(UPCOMING_FILE, upcoming)
        log(f"[*] Saved {len(upcoming)} entries to upcoming_mints.json")
    else:
        log("\n[Step 3-4/5] No new slugs, skipping eligibility check")
        save_json(UPCOMING_FILE, upcoming)

    # ── Step 5: Send reports ──
    log("\n[Step 5/5] Sending Telegram reports...")
    upcoming_report = format_upcoming_report(upcoming)
    today_report = format_today_report(upcoming)

    log("\n--- Upcoming Mints ---")
    print(upcoming_report)
    log("---")

    if upcoming_report:
        send_telegram(upcoming_report)
    else:
        send_telegram("*📋 Upcoming Mints*\n\n_Tidak ada upcoming mint._")

    if today_report:
        log("\n--- Mint Today ---")
        print(today_report)
        log("---")
        send_telegram(today_report)

    log(f"\n[*] Pipeline complete! ({len(upcoming)} active projects)")


if __name__ == "__main__":
    main()
