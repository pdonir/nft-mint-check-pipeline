"""
OpenSea NFT Eligibility Checker (Multi-Wallet, Multi-Collection).

Outputs grouped by PROJECT with all wallets nested, all stages with ✅/❌:

  1. ProjectName - Chain - https://opensea.io/collection/<slug>/overview
  - Wallet 1
  ✅/❌ Tier - price - limit - date GMT+7
  ✅/❌ ...
  - Wallet 2
  ✅/❌ Tier - price - limit - date GMT+7

Usage:
  python3 opensea_eligibility_browser_batch.py <wallet_csv> <slug_csv>
  python3 opensea_eligibility_browser_batch.py wallet_1,wallet_2 slug1,slug2

Wallet keys are the keys in your `wallets.json` (e.g. `wallet_1`, `wallet_2`).
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.async_api import async_playwright


def _load_rabby_password():
    """Load Rabby extension unlock password from env or secrets file.

    Priority:
    1. $RABBY_PASSWORD env var (already exported in shell)
    2. $RABBY_SECRETS_FILE (default `/path/to/rabby_password.env`, file with
       KEY='value' lines, perms 600)

    Returns the password string. Raises SystemExit if neither source is set.
    """
    pw = os.environ.get("RABBY_PASSWORD")
    if pw:
        return pw
    secrets_file = Path(os.environ.get("RABBY_SECRETS_FILE", "/path/to/rabby_password.env"))
    if secrets_file.exists():
        try:
            for raw in secrets_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "RABBY_PASSWORD":
                    return value.strip().strip("'\"")
        except Exception as exc:
            raise SystemExit(f"Failed to read {secrets_file}: {exc}")
    raise SystemExit(
        "RABBY_PASSWORD not set. Either export it or create "
        "`$RABBY_SECRETS_FILE` (default `/path/to/rabby_password.env`, chmod 600)."
    )


# ============ Config ============
EXTENSION_DIR = Path(os.environ.get("RABBY_EXTENSION_DIR", "/path/to/rabby_extension")).resolve()
PROFILE_BASE = Path(os.environ.get("OPENSEA_PROFILE_BASE", "/path/to/browser_profiles"))
RABBY_PASSWORD = _load_rabby_password()

# Wallet name → display label (matches user's preferred output format)
# Loaded from `config/wallets.json` next to the workload root. Searched from
# this script's dir upward so the checker works whether run in-place or
# copied under a different tree (nft-trade, etc.).
def _load_wallet_labels():
    """Load wallet display labels from the nearest config/wallets.json."""
    script_dir = Path(__file__).resolve().parent
    for parent in [script_dir, *script_dir.parents]:
        candidate = parent / "config" / "wallets.json"
        if candidate.exists():
            try:
                with candidate.open() as f:
                    data = json.load(f)
                return {
                    k: v.get("display", k)
                    for k, v in data.items()
                    if isinstance(v, dict)
                }
            except Exception as exc:
                print(f"[!] Failed to load {candidate}: {exc}")
                return {}
    return {}


WALLET_LABELS = _load_wallet_labels()

GMT7 = timezone(timedelta(hours=7))


# ============ Rabby popup handling ============
async def find_rabby_popup(context, timeout_s=12):
    for _ in range(timeout_s * 2):
        for pg in context.pages:
            if "notification.html" in pg.url:
                return pg
        await asyncio.sleep(0.5)
    return None


async def handle_rabby_popup(popup, password=None):
    try:
        await popup.bring_to_front()
        await popup.wait_for_timeout(2000)
        url = popup.url

        if "unlock" in url and password:
            try:
                pw = popup.locator('input[type="password"]').first
                await pw.wait_for(state="visible", timeout=5000)
                await pw.fill(password)
                await popup.wait_for_timeout(500)
                await popup.locator('button:has-text("Unlock")').first.click()
                await popup.wait_for_timeout(3500)
            except Exception:
                pass

        for label in ["Sign", "Confirm", "Connect", "Allow", "Approve"]:
            try:
                btn = popup.locator(f'button:has-text("{label}")').first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await popup.wait_for_timeout(2500)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def wait_handle_all_popups(context, password, max_rounds=4):
    for _ in range(max_rounds):
        popup = await find_rabby_popup(context, timeout_s=8)
        if not popup:
            break
        await handle_rabby_popup(popup, password)
        await asyncio.sleep(2)


# ============ Login flow ============
async def is_wallet_connected(page):
    """Check if wallet is actually connected by looking for wallet address or profile elements.
    More reliable than just checking if 'Connect Wallet' button is absent."""
    try:
        text = await page.evaluate("() => document.body.innerText")
        # If Connect Wallet button is visible, definitely not connected
        if "Connect Wallet" in text:
            return False
        # Check for wallet address pattern (0x...) - most reliable indicator
        if re.search(r"0x[a-fA-F0-9]{4,}", text):
            return True
        # Check for profile-specific elements
        if "Edit Profile" in text or "My Collections" in text:
            return True
        # Check for eligibility labels on collection page
        if "ELIGIBLE" in text or "NOT ELIGIBLE" in text:
            return True
        # Check for wallet count or profile name indicators
        if "WALLETS" in text or "USD VALUE" in text:
            return True
        return False
    except Exception:
        return False


async def is_connect_wallet_visible(page):
    """Reliable signal session expired: Connect Wallet button visible as button."""
    try:
        btn = page.locator('button:has-text("Connect Wallet")').first
        return await btn.is_visible(timeout=2000)
    except Exception:
        return False


async def explicit_rabby_unlock(context, password):
    """Explicitly unlock Rabby extension if locked. Opens Rabby popup and enters password."""
    try:
        # Find Rabby service worker
        sw = None
        for s in context.service_workers:
            if "rabby" in s.url.lower() or "extension" in s.url.lower():
                sw = s
                break
        if not sw:
            return False

        ext_id = sw.url.split("/")[2]
        # Open Rabby popup
        unlock_page = await context.new_page()
        await unlock_page.goto(f"chrome-extension://{ext_id}/index.html#/unlock",
                               wait_until="domcontentloaded", timeout=15000)
        await unlock_page.wait_for_timeout(3000)

        text = await unlock_page.evaluate("() => document.body.innerText")

        # If already unlocked (no password field), close and return
        if "Unlock" not in text and "Password" not in text:
            await unlock_page.close()
            print("    [+] Rabby already unlocked")
            return True

        # Enter password
        try:
            pw_input = unlock_page.locator('input[type="password"]').first
            await pw_input.wait_for(state="visible", timeout=5000)
            await pw_input.fill(password)
            await unlock_page.wait_for_timeout(500)
            unlock_btn = unlock_page.locator('button:has-text("Unlock")').first
            await unlock_btn.click()
            await unlock_page.wait_for_timeout(3000)
            after_text = await unlock_page.evaluate("() => document.body.innerText")
            if "Cannot unlock without a previous vault" in after_text:
                print("    [!] Rabby has no previous vault in this browser profile")
                await unlock_page.close()
                return False
            if "Unlock" in after_text and "Password" in after_text:
                print("    [!] Rabby still locked after unlock attempt")
                await unlock_page.close()
                return False
            print("    [+] Rabby unlocked")
        except Exception as e:
            print(f"    [!] Rabby unlock failed: {e}")

        await unlock_page.close()
        return True
    except Exception as e:
        print(f"    [!] explicit_rabby_unlock error: {e}")
        return False


async def ensure_session_active(page, context, password):
    """If Connect Wallet button visible, perform full connect + SIWE sign flow.
    Returns True if login was performed, False if session was already active."""
    if not await is_connect_wallet_visible(page):
        return False

    print("    [*] session expired, reconnecting wallet...")
    try:
        connect_btn = page.locator('button:has-text("Connect Wallet")').first
        await connect_btn.click()
        await page.wait_for_timeout(3000)
    except Exception:
        pass

    # Pick Rabby in modal
    try:
        rabby_btn = page.locator('button:has-text("Rabby Wallet")').first
        await rabby_btn.wait_for(state="visible", timeout=10000)
        await rabby_btn.click()
    except Exception:
        try:
            await page.mouse.click(640, 345)
        except Exception:
            pass

    await page.wait_for_timeout(4000)
    # Handle all popups: unlock → connect → sign SIWE
    await wait_handle_all_popups(context, password, max_rounds=5)
    await page.wait_for_timeout(5000)
    return True


async def force_login_via_profile(page, context, password):
    """Force fresh SIWE login by navigating to /profile (most reliable trigger).
    Returns True if login succeeded, False otherwise."""
    print("    [*] forcing fresh login via /profile...")
    await page.goto("https://opensea.io/profile", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(10000)  # Wait longer for page to fully render

    # Log page state
    text = await page.evaluate("() => document.body.innerText")
    has_connect = "Connect Wallet" in text
    print(f"    [DEBUG] page has 'Connect Wallet': {has_connect}")

    # ALWAYS try to click Connect Wallet first, wait for it to appear
    try:
        connect_btn = page.locator('button:has-text("Connect Wallet")').first
        await connect_btn.wait_for(state="visible", timeout=15000)
        await connect_btn.click()
        print(f"    [+] clicked Connect Wallet button")
        await page.wait_for_timeout(5000)
    except Exception:
        # If no Connect Wallet button, try clicking the profile area to trigger modal
        print(f"    [!] no Connect Wallet button visible, trying alternative triggers...")
        # Try clicking on "Sign in" or profile avatar area
        try:
            sign_in = page.locator('button:has-text("Sign in")').first
            if await sign_in.is_visible(timeout=3000):
                await sign_in.click()
                print(f"    [+] clicked Sign in button")
                await page.wait_for_timeout(3000)
        except Exception:
            pass

    # Now try Rabby Wallet button in modal (wait longer)
    try:
        rabby_btn = page.locator('button:has-text("Rabby Wallet")').first
        await rabby_btn.wait_for(state="visible", timeout=15000)
        await rabby_btn.click()
        print(f"    [+] clicked Rabby Wallet button in modal")
    except Exception as e:
        print(f"    [!] no Rabby Wallet button in modal: {e}")
        # Try alternative: look for Rabby in different ways
        try:
            # Try clicking by text content via JS (more flexible)
            clicked = await page.evaluate("""() => {
                const all = document.querySelectorAll('button, div[role="button"], div[class*="wallet"], div[class*="option"]');
                for (const el of all) {
                    const t = (el.innerText || '').trim();
                    if (t.includes('Rabby') || t.includes('rabby')) {
                        el.click();
                        return 'rabby_found';
                    }
                }
                // Try looking for any wallet option that's not Coinbase
                for (const el of all) {
                    const t = (el.innerText || '').trim();
                    if (t && t !== 'Coinbase Wallet' && t !== 'MetaMask' && t.includes('Wallet')) {
                        el.click();
                        return 'other_wallet: ' + t;
                    }
                }
                return null;
            }""")
            if clicked:
                print(f"    [+] clicked wallet via JS: {clicked}")
                await page.wait_for_timeout(3000)
            else:
                # Last resort: close modal, try keyboard shortcut
                print(f"    [!] no wallet found in modal, trying Escape + keyboard shortcut...")
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(2000)
                # Try Ctrl+Shift+E (Rabby shortcut) or other methods
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(1000)
        except Exception as e2:
            print(f"    [!] alternative wallet click failed: {e2}")

    await page.wait_for_timeout(5000)

    # Check for Rabby popup explicitly
    popup = await find_rabby_popup(context, timeout_s=8)
    if popup:
        print(f"    [+] Rabby popup detected: {popup.url}")
        try:
            await popup.bring_to_front()
            await popup.wait_for_timeout(2000)
            popup_text = await popup.evaluate("() => document.body.innerText")
            print(f"    [DEBUG] popup text preview: {popup_text[:200]}")
            # Try to click Sign/Confirm directly on this popup
            for label in ["Sign", "Confirm", "Connect", "Allow", "Approve"]:
                try:
                    btn = popup.locator(f'button:has-text("{label}")').first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        print(f"    [+] clicked '{label}' button on popup")
                        await popup.wait_for_timeout(3000)
                        break
                except Exception:
                    continue
            else:
                # Try JS click for non-standard buttons
                clicked = await popup.evaluate("""() => {
                    const btns = document.querySelectorAll('button, div[role="button"], a');
                    for (const b of btns) {
                        const t = (b.innerText || '').trim();
                        if (t.includes('Sign') || t.includes('Confirm') || t.includes('Approve')) {
                            b.click();
                            return t;
                        }
                    }
                    return null;
                }""")
                if clicked:
                    print(f"    [+] clicked via JS: {clicked}")
                    await popup.wait_for_timeout(3000)
        except Exception as e:
            print(f"    [!] popup handling failed: {e}")
    else:
        print(f"    [!] NO Rabby popup detected")
        for i, pg in enumerate(context.pages):
            print(f"    [DEBUG] page {i}: {pg.url}")

    await wait_handle_all_popups(context, password, max_rounds=5)
    await page.wait_for_timeout(5000)

    # Verify login actually worked
    connected = await is_wallet_connected(page)
    if connected:
        print("    [+] LOGIN OK - wallet connected")
    else:
        print("    [!] LOGIN FAILED - wallet not connected")
    return connected


async def robust_login(page, context, password, wallet_name, max_retries=2):
    """Robust login with retry and browser restart capability."""
    for attempt in range(max_retries):
        # Step 1: Explicit Rabby unlock first
        await explicit_rabby_unlock(context, password)
        await page.wait_for_timeout(2000)

        # Step 2: Navigate to profile for SIWE
        success = await force_login_via_profile(page, context, password)
        if success:
            return True

        print(f"    [!] Login attempt {attempt + 1} failed, retrying...")
        await page.wait_for_timeout(3000)

    print(f"    [!] LOGIN FAILED after {max_retries} attempts")
    return False


# ============ Eligibility extraction ============
def parse_utc_to_gmt7(utc_str):
    """Normalize any OpenSea time string to '28 May 22:00 GMT+7'.

    OpenSea renders mint stage times in several variants depending on
    browser locale and page version. Examples observed:
      - 'May 28 at 3:00 PM UTC'          (en-US, UTC)
      - 'June 15 at 9:00 PM GMT+7'       (en-US, browser TZ = Asia/Jakarta)
      - '15 Jun at 21:00 GMT+7'          (en-GB style, 24h)
      - '28 May 22:00 GMT+7'             (already normalized — pass through)
      - '2026-06-15T14:00:00Z' / '...+07:00' (ISO)

    All variants are converted to GMT+7 and rendered as '%d %b %H:%M GMT+7'.
    Returns original string only if every parser fails (so caller still sees
    *something* in the report instead of an empty cell).
    """
    if not utc_str:
        return ""
    s = utc_str.strip()

    # Already normalized? cheap shortcut.
    if re.fullmatch(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{2}:\d{2}\s+GMT\+7", s):
        return s

    # ── 1. ISO 8601 (with Z or ±HH:MM) ──
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(GMT7).strftime("%d %b %H:%M GMT+7")
    except (ValueError, TypeError):
        pass

    year = datetime.utcnow().year

    # ── 2. Extract trailing timezone token ──
    # Matches: 'UTC', 'GMT', 'GMT+7', 'GMT-5', 'UTC+0'
    tz_match = re.search(r"\b(UTC|GMT)([+-]\d{1,2})?(?::?\d{2})?\s*$", s, re.IGNORECASE)
    if tz_match:
        tz_label = tz_match.group(1).upper()
        tz_offset_h = int(tz_match.group(2)) if tz_match.group(2) else 0
        src_tz = timezone(timedelta(hours=tz_offset_h)) if tz_label in ("UTC", "GMT") else timezone.utc
        body = s[: tz_match.start()].strip()
    else:
        # No timezone token → assume already GMT+7 (matches xvfb default)
        src_tz = GMT7
        body = s

    # Drop optional 'at' connector: 'May 28 at 3:00 PM' → 'May 28 3:00 PM'
    body = re.sub(r"\s+at\s+", " ", body, flags=re.IGNORECASE).strip()

    # ── 3. Try multiple date formats ──
    fmts = [
        "%B %d %I:%M %p",   # June 15 9:00 PM
        "%b %d %I:%M %p",   # Jun 15 9:00 PM
        "%B %d %H:%M",      # June 15 21:00
        "%b %d %H:%M",      # Jun 15 21:00
        "%d %B %I:%M %p",   # 15 June 9:00 PM
        "%d %b %I:%M %p",   # 15 Jun 9:00 PM
        "%d %B %H:%M",      # 15 June 21:00
        "%d %b %H:%M",      # 15 Jun 21:00
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(f"{body} {year}", f"{fmt} %Y")
            dt = dt.replace(tzinfo=src_tz)
            # If parsed date is far in the past, assume it's next year
            if dt < datetime.now(src_tz) - timedelta(days=180):
                dt = dt.replace(year=year + 1)
            return dt.astimezone(GMT7).strftime("%d %b %H:%M GMT+7")
        except ValueError:
            continue

    return utc_str  # final fallback — caller will see raw string


def parse_eligibility(page_text, slug):
    """
    Parse mint stages from OpenSea collection page.
    Returns: {project, chain, stages: [{name, start, end, price_eth, limit, eligible}]}
    """
    lines = [l.strip() for l in page_text.split("\n")]

    # Find project name (line after "All collections...")
    project_name = slug
    chain = "Ethereum"
    for i, ln in enumerate(lines):
        if ln.startswith("All collections") and i + 1 < len(lines):
            project_name = lines[i + 1].strip() or slug
            # Chain is usually 2 lines later
            if i + 2 < len(lines) and lines[i + 2].strip():
                ch = lines[i + 2].strip()
                if ch.upper() in ("ETHEREUM", "BASE", "POLYGON", "ARBITRUM", "OPTIMISM",
                                  "ZKSYNC", "LINEA", "BSC", "AVALANCHE"):
                    chain = ch.capitalize()
            break

    if "MINT SCHEDULE" not in page_text:
        return {"project": project_name, "chain": chain, "stages": [], "error": "no mint schedule"}

    sched_idx = lines.index("MINT SCHEDULE")
    end_markers = ["LIVE MINTS", "LIVE SALES", "Live", "Aggregating"]
    end_idx = len(lines)
    for m in end_markers:
        if m in lines[sched_idx:]:
            cand = sched_idx + lines[sched_idx:].index(m)
            if cand < end_idx:
                end_idx = cand

    block = lines[sched_idx + 1 : end_idx]

    stages = []
    i = 0
    while i < len(block):
        ln = block[i]
        if not ln or ln in ("ETH",) or ln.startswith("|") or re.match(r"^\d+\.\d+$", ln):
            i += 1
            continue
        if ln.startswith("Starts:") or ln.startswith("Ends:") or "LIMIT" in ln:
            i += 1
            continue
        if ln in ("ELIGIBLE", "NOT ELIGIBLE"):
            i += 1
            continue

        # New stage
        stage = {"name": ln, "start": "", "end": "", "price_eth": "", "limit": "", "eligible": None}
        i += 1
        while i < len(block):
            cur = block[i]
            # OpenSea page text uses both "Starts: ..." and "Started: ..." —
            # accept both so we don't fall through to the "next stage name" catch-all.
            if cur.startswith(("Starts:", "Started:")):
                stage["start"] = re.sub(r"^(Starts|Started):\s*", "", cur).strip()
            elif cur.startswith(("Ends:", "Ended:")):
                stage["end"] = re.sub(r"^(Ends|Ended):\s*", "", cur).strip()
            # OpenSea renders price as either "0.0001" or "< 0.0001" (when the
            # price is below the precision threshold). Accept both.
            elif re.match(r"^[<>]?\s*\d+\.\d+$", cur):
                stage["price_eth"] = re.sub(r"^[<>]\s*", "", cur).strip()
            elif cur == "ETH":
                pass
            elif "LIMIT" in cur and "PER WALLET" in cur:
                m = re.search(r"LIMIT (\d+)", cur)
                if m:
                    stage["limit"] = m.group(1)
            elif cur in ("ELIGIBLE", "NOT ELIGIBLE"):
                stage["eligible"] = cur == "ELIGIBLE"
                i += 1
                break
            elif cur and not cur.startswith("|") and not cur.startswith("0.") and cur != "ETH":
                # Possibly next stage name, back up
                break
            i += 1
        if stage["name"]:
            stages.append(stage)

    return {"project": project_name, "chain": chain, "stages": stages}



def format_stage_line(stage):
    """Format single stage line: ✅/❌ Tier (price, limit) — date GMT+7.

    Free mints (no price_eth) render as "(FREE, limit N)" instead of "(0.00 ETH, ...)"
    so Telegram readers don't get confused by zero-priced drops.
    """
    icon = "✅" if stage["eligible"] else "❌"
    limit = stage["limit"] or "?"
    when = parse_utc_to_gmt7(stage["start"])
    if stage["price_eth"] and float(stage["price_eth"]) > 0:
        price_str = f"{stage['price_eth']} ETH"
    else:
        price_str = "FREE"
    if when:
        return f"{icon} {stage['name']} ({price_str}, limit {limit}) — {when}"
    else:
        return f"{icon} {stage['name']}"


# ============ Main batch ============
async def check_one(page, slug, context=None, password=None, wallet_name=None):
    """Check eligibility for one slug. Self-healing: if session expired
    or eligibility labels never load, auto re-login and retry once."""

    async def navigate_and_poll():
        await page.goto(f"https://opensea.io/collection/{slug}/overview",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)

        # Detect session expired BEFORE polling. OpenSea can still show public
        # eligibility labels while disconnected, so do not trust labels when the
        # Connect Wallet button is visible.
        if context and password and await is_connect_wallet_visible(page):
            await robust_login(page, context, password, wallet_name or "check_one", max_retries=1)
            await page.goto(f"https://opensea.io/collection/{slug}/overview",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(10000)

        # Debug: check page state
        text_init = await page.evaluate("() => document.body.innerText")
        has_mint = "MINT SCHEDULE" in text_init
        has_connect = "Connect Wallet" in text_init
        has_elig = "ELIGIBLE" in text_init or "NOT ELIGIBLE" in text_init
        print(f"    [DEBUG] {slug}: MINT_SCHEDULE={has_mint}, Connect_Wallet={has_connect}, ELIGIBLE={has_elig}")

        if not has_mint or not has_elig:
            # Page might not have loaded properly, refresh and wait
            print(f"    [*] {slug}: data not loaded, refreshing...")
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(15000)  # Wait longer after refresh

        # Poll until eligibility labels start appearing (don't wait for ALL)
        text = ""
        for attempt in range(15):  # 15 × 3s = 45s max
            text = await page.evaluate("() => document.body.innerText")
            if "MINT SCHEDULE" not in text:
                await page.wait_for_timeout(3000)
                continue

            # Extract MINT SCHEDULE section only (not whole page)
            mint_start = text.index("MINT SCHEDULE")
            # Find end: next section header or max 2000 chars
            mint_section = text[mint_start:mint_start+2000]
            # Cut at common section boundaries
            for end_marker in ["LIVE MINTS", "LIVE SALES", "ITEM", "Activity\n", "Status\n"]:
                if end_marker in mint_section:
                    mint_section = mint_section[:mint_section.index(end_marker)]
                    break

            stage_count = mint_section.count("LIMIT ")
            not_elig_count = mint_section.count("NOT ELIGIBLE")
            elig_count = mint_section.count("ELIGIBLE")
            pure_elig = elig_count - not_elig_count
            total_labels = elig_count

            if attempt % 5 == 0:
                print(f"    [DEBUG] attempt {attempt}: stages={stage_count}, ELIGIBLE={pure_elig}, NOT_ELIG={not_elig_count}")

            # Accept if at least 1 label loaded (don't wait for all)
            if total_labels >= 1:
                # Wait a bit more for additional labels to load
                await page.wait_for_timeout(3000)
                text = await page.evaluate("() => document.body.innerText")
                print(f"    [+] labels loaded: {total_labels} labels for {stage_count} stages")
                break
            await page.wait_for_timeout(3000)

        still_disconnected = context and password and await is_connect_wallet_visible(page)
        labels_loaded = (
            "MINT SCHEDULE" in text
            and ("ELIGIBLE" in text or "NOT ELIGIBLE" in text)
            and not still_disconnected
        )
        return text, labels_loaded

    text, labels_loaded = await navigate_and_poll()

    # Retry path: if labels never loaded, session likely silently broken.
    # Force re-login with robust method and retry once.
    if not labels_loaded and context and password:
        print(f"    [!] {slug}: authenticated eligibility missing, forcing re-login...")
        await robust_login(page, context, password, wallet_name or "retry", max_retries=1)
        text, labels_loaded = await navigate_and_poll()

    parsed = parse_eligibility(text, slug)
    if not labels_loaded:
        parsed["error"] = "authenticated eligibility not loaded"
    return parsed


async def check_wallet(wallet_name, slugs):
    """Check all slugs for one wallet, return dict {slug: parsed}."""
    profile_dir = PROFILE_BASE / f"opensea_{wallet_name}"
    if not profile_dir.exists():
        print(f"[!] Profile not found: {profile_dir}")
        return {}

    # Clear stale singleton locks from killed/crashed previous runs
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock = profile_dir / lock_name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except Exception:
            pass

    print(f"\n[*] === Wallet: {WALLET_LABELS.get(wallet_name, wallet_name)} ===")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        for _ in range(60):
            if context.service_workers:
                break
            await asyncio.sleep(0.5)

        # DON'T clear cookies — session persist in browser profile
        # Just unlock Rabby and navigate

        page = await context.new_page()

        # Unlock Rabby first (like opening browser with extension active)
        print(f"[*] {wallet_name}: unlocking Rabby...")
        await explicit_rabby_unlock(context, RABBY_PASSWORD)
        await page.wait_for_timeout(2000)

        # Navigate to first collection
        await page.goto(f"https://opensea.io/collection/{slugs[0]}/overview",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(10000)

        # Check if balance visible (indicates session valid)
        text = await page.evaluate("() => document.body.innerText")
        has_connect = "Connect Wallet" in text
        has_elig = "ELIGIBLE" in text or "NOT ELIGIBLE" in text
        print(f"[*] {wallet_name}: Connect_Wallet={has_connect}, ELIGIBLE={has_elig}")

        if has_connect and not has_elig:
            # Session expired — need fresh full-browser Rabby connect/SIWE sign.
            print(f"[*] {wallet_name}: session expired, performing browser SIWE login...")
            login_ok = await robust_login(page, context, RABBY_PASSWORD, wallet_name, max_retries=2)
            if login_ok:
                await page.goto(f"https://opensea.io/collection/{slugs[0]}/overview",
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(10000)
                print(f"[+] {wallet_name}: reloaded with fresh session")
            else:
                print(f"[!] {wallet_name}: login failed, trying to continue...")
        elif not has_elig:
            # Session might be valid but page didn't load eligibility — refresh
            print(f"[*] {wallet_name}: no eligibility data, refreshing...")
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(10000)
        else:
            print(f"[+] {wallet_name}: session active, eligibility data loaded")

        results = {}
        for i, slug in enumerate(slugs):
            print(f"  [{i+1}/{len(slugs)}] {slug}")
            try:
                results[slug] = await check_one(page, slug, context, RABBY_PASSWORD, wallet_name)
            except Exception as e:
                results[slug] = {"error": str(e)}

        await context.close()
        return results


def render_report(all_results, slugs, wallet_names):
    """Render final grouped report.
    all_results: {wallet_name: {slug: parsed}}
    """
    lines = []
    for idx, slug in enumerate(slugs, start=1):
        # Use first wallet's data for project name + chain (same across wallets)
        project_data = None
        for w in wallet_names:
            if slug in all_results.get(w, {}) and "project" in all_results[w][slug]:
                project_data = all_results[w][slug]
                break
        if not project_data:
            lines.append(f"{idx}. {slug} - ERROR: no data")
            continue

        link = f"https://opensea.io/collection/{slug}/overview"
        lines.append(f"{idx}. [{project_data['project']}]({link}) — {project_data['chain']}")

        for w in wallet_names:
            label = WALLET_LABELS.get(w, w)
            data = all_results.get(w, {}).get(slug, {})
            lines.append(f"**{label}:**")
            if data.get("error"):
                lines.append(f"  (no data: {data['error']})")
                continue
            stages = data.get("stages", [])
            if not stages:
                err = data.get("error", "no stages parsed")
                lines.append(f"  (no data: {err})")
                continue
            for s in stages:
                lines.append(format_stage_line(s))
        lines.append("")  # blank between projects
    return "\n".join(lines)


async def main(wallet_names, slugs):
    all_results = {}
    for w in wallet_names:
        all_results[w] = await check_wallet(w, slugs)

    report = render_report(all_results, slugs, wallet_names)

    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print(report)

    out = Path("/tmp/elig_report.txt")
    out.write_text(report)
    print(f"\n[*] Saved to {out}")
    return report


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 opensea_eligibility_browser_batch.py <wallet_csv> <slug_csv>")
        print("Example: python3 opensea_checker_browser_batch.py wallet_1,wallet_2 slug1,slug2")
        print("Wallets:", list(WALLET_LABELS.keys()))
        sys.exit(1)

    def normalize_slug(value):
        value = value.strip()
        if "opensea.io/collection/" in value:
            value = value.split("opensea.io/collection/", 1)[1]
        return value.split("/", 1)[0]

    wallet_names = [w.strip().lower() for w in sys.argv[1].split(",") if w.strip()]
    slugs = [normalize_slug(s) for s in sys.argv[2].split(",") if s.strip()]
    if len(sys.argv) > 3:
        # Backward compat: positional slugs
        slugs = [normalize_slug(s) for s in sys.argv[2:]]
    asyncio.run(main(wallet_names, slugs))
