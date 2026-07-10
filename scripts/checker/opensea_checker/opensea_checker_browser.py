"""
OpenSea + Rabby + Full Eligibility Detail (v2 - robust login).

Auto-detects login state, re-logs in if needed, then captures eligibility detail.
"""
import asyncio
import os
import sys
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
PROFILE_DIR = Path(os.environ.get("OPENSEA_PROFILE_DIR", "/path/to/browser_profiles/opensea"))
RABBY_PASSWORD = _load_rabby_password()
COLLECTION_SLUG = sys.argv[1].strip().removeprefix("https://opensea.io/collection/").split("/")[0] if len(sys.argv) > 1 else ""
if not COLLECTION_SLUG:
    raise SystemExit("Usage: python3 opensea_checker_browser.py <collection_slug_or_opensea_url>")


async def dump(page, label):
    try:
        await page.screenshot(path=f"/tmp/os_{label}.png", full_page=False)
        text = await page.evaluate("() => document.body.innerText")
        print(f"\n=== {label} ===")
        print(f"URL: {page.url}")
        print(f"TEXT: {text[:1500]}")
    except Exception as e:
        print(f"[!] dump fail {label}: {e}")


async def find_rabby_popup(context, timeout_s=15):
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
        text = await popup.evaluate("() => document.body.innerText")
        print(f"[Rabby] {url}")
        print(f"[Rabby TEXT] {text[:300]}")

        if "unlock" in url and password:
            pw = popup.locator('input[type="password"]').first
            await pw.wait_for(state="visible", timeout=5000)
            await pw.fill(password)
            await popup.wait_for_timeout(500)
            await popup.locator('button:has-text("Unlock")').first.click()
            await popup.wait_for_timeout(3500)
            try:
                url = popup.url
                text = await popup.evaluate("() => document.body.innerText")
                if "Cannot unlock without a previous vault" in text:
                    print("[!] Rabby has no previous vault in this browser profile")
                    return False
                print("[+] Rabby unlocked")
                print(f"[Rabby after unlock] {url}")
            except Exception:
                print("[+] Rabby unlocked")
                return True

        for label in ["Sign", "Confirm", "Connect", "Allow", "Approve"]:
            try:
                btn = popup.locator(f'button:has-text("{label}")').first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    print(f"[+] Rabby clicked: {label}")
                    await popup.wait_for_timeout(2500)
                    return True
            except Exception:
                continue
    except Exception as e:
        print(f"[!] handle err: {e}")
    return False


async def wait_handle_all_popups(context, password, max_rounds=4):
    handled_any = False
    for r in range(max_rounds):
        popup = await find_rabby_popup(context, timeout_s=8)
        if not popup:
            print(f"[*] Round {r+1}: no popup")
            break
        print(f"[*] Round {r+1}: handling popup")
        if await handle_rabby_popup(popup, password):
            handled_any = True
        await asyncio.sleep(2)
    return handled_any


async def is_logged_in(page):
    """Check if already logged in to OpenSea."""
    text = await page.evaluate("() => document.body.innerText")
    # If "Connect Wallet" still visible in main content area, NOT logged in
    # If logged in, profile shows wallet address
    return "0x" in text and ("USD VALUE" in text or "/profile" in page.url)


async def login_opensea(page, context, password):
    """Trigger SIWE login flow."""
    print("[*] Triggering OpenSea login (navigate to /profile)...")
    # Navigate directly to /profile — this auto-opens login modal
    await page.goto("https://opensea.io/profile", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(10000)

    # Click Rabby Wallet button — explicit wait
    try:
        rabby_btn = page.locator('button:has-text("Rabby Wallet")').first
        await rabby_btn.wait_for(state="visible", timeout=15000)
        await rabby_btn.click()
        print("[+] Clicked Rabby Wallet button")
    except Exception as e:
        print(f"[!] Rabby button click failed: {e}")
        # Try mouse click at debug coordinates
        await page.mouse.click(640, 345)
        print("[+] Fallback: clicked at (640, 345)")

    await page.wait_for_timeout(5000)

    # Handle popups: unlock + connect + sign
    await wait_handle_all_popups(context, password, max_rounds=4)
    await page.wait_for_timeout(5000)


async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        sw = None
        for _ in range(60):
            if context.service_workers:
                sw = context.service_workers[0]
                break
            await asyncio.sleep(0.5)
        ext_id = sw.url.split("/")[2]
        print(f"[+] Rabby ext: {ext_id}")

        page = await context.new_page()
        await page.goto(f"https://opensea.io/collection/{COLLECTION_SLUG}/overview",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)

        # Check if logged in already
        if not await is_logged_in(page):
            print("[*] Not logged in, starting login flow")
            await login_opensea(page, context, RABBY_PASSWORD)
            await page.goto(f"https://opensea.io/collection/{COLLECTION_SLUG}/overview",
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(8000)
        else:
            print("[+] Already logged in")

        await dump(page, "01_logged_in")

        # Click "View eligibility"
        clicked = await page.evaluate("""() => {
            const els = document.querySelectorAll('button, a, [role="button"], span, div');
            for (const el of els) {
                if (el.innerText.trim() === 'View eligibility' && el.offsetParent !== null) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        print(f"[+] Clicked View eligibility: {clicked}")
        await page.wait_for_timeout(6000)
        await dump(page, "02_eligibility_modal")

        # Look for Sign in button (auth modal)
        signed = await page.evaluate("""() => {
            const els = document.querySelectorAll('button, [role="button"], a');
            for (const el of els) {
                const t = el.innerText.trim();
                if ((t === 'Sign in' || t === 'Sign In' || t === 'Authenticate' || t === 'Continue') && el.offsetParent !== null) {
                    el.click();
                    return t;
                }
            }
            return null;
        }""")
        print(f"[+] Auth button click: {signed}")
        await page.wait_for_timeout(4000)

        # Handle Rabby sign popup
        await wait_handle_all_popups(context, RABBY_PASSWORD, max_rounds=3)
        await page.wait_for_timeout(6000)

        await dump(page, "03_eligibility_after_auth")

        # Capture eligibility content from modal/dialog
        elig = await page.evaluate("""() => {
            const candidates = [
                ...document.querySelectorAll('[role="dialog"]'),
                ...document.querySelectorAll('[class*="Modal"]'),
                ...document.querySelectorAll('[class*="modal"]'),
                ...document.querySelectorAll('[class*="Drawer"]'),
                ...document.querySelectorAll('[class*="Dialog"]'),
                ...document.querySelectorAll('[class*="dialog"]'),
            ];
            const visible = candidates.filter(el =>
                el.offsetParent !== null && el.innerText.length > 30
            );
            visible.sort((a, b) => b.innerText.length - a.innerText.length);
            return visible.slice(0, 3).map(el => el.innerText);
        }""")

        print("\n" + "=" * 70)
        print("ELIGIBILITY DETAIL")
        print("=" * 70)
        if elig:
            for i, t in enumerate(elig):
                print(f"\n--- Modal #{i+1} ({len(t)} chars) ---")
                print(t)
        else:
            print("(no modal)")
        print("=" * 70)

        # Save full page
        full_text = await page.evaluate("() => document.body.innerText")
        with open("/tmp/elig_page.txt", "w") as f:
            f.write(full_text)
        print(f"\n[*] Full page saved /tmp/elig_page.txt ({len(full_text)} chars)")

        print("[*] Pausing 30s...")
        await page.wait_for_timeout(30000)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
