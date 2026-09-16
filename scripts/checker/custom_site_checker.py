#!/usr/bin/env python3
"""Generic non-OpenSea NFT eligibility checker for simple address-input sites."""
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

WORKLOAD_ROOT = Path(__file__).resolve().parents[2]  # workload root
BASE_DIR = WORKLOAD_ROOT  # alias kept for downstream usages
CONFIG_DIR = WORKLOAD_ROOT / "config"
STATE_DIR = WORKLOAD_ROOT / "state"
WALLETS_FILE = CONFIG_DIR / "wallets.json"
# WALLET_DISPLAY loaded dynamically from wallets.json
WALLET_DISPLAY = {}
CHAIN_HINTS = {
    'ethereum': 'Ethereum',
    'eth': 'Ethereum',
    'base': 'Base',
    'polygon': 'Polygon',
    'arbitrum': 'Arbitrum',
    'optimism': 'Optimism',
    'bsc': 'BSC',
    'avalanche': 'Avalanche',
    'fantom': 'Fantom',
    'linea': 'Linea',
    'zksync': 'zkSync',
}


def load_wallet_addresses():
    global WALLET_DISPLAY
    with open(WALLETS_FILE) as f:
        wallets_data = json.load(f)
    if isinstance(wallets_data, dict):
        # New format: {"wallet_key": {"display": "Name"}, ...}
        WALLET_DISPLAY = {k: v.get("display", k) for k, v in wallets_data.items()}
        wallet_names = list(wallets_data.keys())
    else:
        # Old format: ["wallet_1", "wallet_2"]
        wallet_names = wallets_data
    with open(Path(os.environ.get('NFT_CONFIG_PATH', '/path/to/nft_config.json'))) as f:
        cfg = json.load(f)
    out = {}
    wallet_map = cfg.get('wallets', {})
    for key in wallet_names:
        display = WALLET_DISPLAY.get(key, key)
        found = None
        for name, meta in wallet_map.items():
            name_norm = name.lower().replace(' ', '').replace('-', '')
            key_norm = display.lower().replace(' ', '').replace('-', '')
            if name_norm == key_norm or key_norm in name_norm or name_norm in key_norm:
                found = meta.get('address')
                break
        if found:
            out[key] = {'display': display, 'address': found}
    return out


def infer_chain(text, url):
    hay = f"{text} {url}".lower()
    for hint, chain in CHAIN_HINTS.items():
        if re.search(rf'\b{re.escape(hint)}\b', hay):
            return chain
    return 'TBA'


def infer_project_name(text, url):
    host = urlparse(url).netloc.lower().replace('www.', '')
    title_hints = []
    for line in text.splitlines()[:30]:
        line = line.strip()
        if 3 <= len(line) <= 80:
            low = line.lower()
            if any(bad in low for bad in ['connect wallet', 'check eligibility', 'eligible', 'not eligible', 'back', 'submit', 'verify']):
                continue
            title_hints.append(line)
    if title_hints:
        return title_hints[0]
    return host.split('.')[0] if host else 'Unknown Project'


def clean_result_text(text):
    lines = []
    seen = set()
    for raw in text.splitlines():
        line = re.sub(r'\s+', ' ', raw).strip()
        if not line:
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        if len(line) > 220:
            continue
        if any(skip in low for skip in [
            'connect wallet', 'follow us', 'twitter', 'discord', 'terms of service',
            'privacy policy', 'powered by', 'copyright', 'menu', 'home'
        ]):
            continue
        lines.append(line)
    return lines


def extract_result_message(before, after, submitted):
    before_lines = clean_result_text(before)
    after_lines = clean_result_text(after)
    before_set = {x.lower() for x in before_lines}
    new_lines = [x for x in after_lines if x.lower() not in before_set]
    candidates = new_lines or after_lines

    priority_patterns = [
        r'public mint only',
        r'not found[^\n]*',
        r'no application[^\n]*',
        r'eligible[^\n]*',
        r'not eligible[^\n]*',
        r'allowlist[^\n]*',
        r'whitelist[^\n]*',
        r'fcfs[^\n]*',
        r'gtd[^\n]*',
        r'public[^\n]*mint[^\n]*',
    ]

    for pattern in priority_patterns:
        for line in candidates:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                return re.sub(r'\s+', ' ', m.group(0)).strip()

    for line in candidates:
        low = line.lower()
        if any(word in low for word in ['found', 'eligible', 'public', 'mint', 'application', 'wallet', 'allowlist', 'whitelist']):
            return line

    if submitted and candidates:
        return candidates[0]
    return 'TBA'


async def try_fill_and_submit(page, address):
    selectors = [
        'input[placeholder*="address" i]',
        'input[name*="address" i]',
        'input[id*="address" i]',
        'input[type="text"]',
        'textarea',
    ]
    filled = False
    for sel in selectors:
        try:
            locator = page.locator(sel).first
            if await locator.is_visible(timeout=1000):
                await locator.fill(address)
                await page.wait_for_timeout(500)
                filled = True
                break
        except Exception:
            continue

    click_texts = ['Check', 'Check Eligibility', 'Verify', 'Submit', 'Search', 'Lookup']
    clicked = False
    for text in click_texts:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await page.wait_for_timeout(3000)
                clicked = True
                break
        except Exception:
            continue
    return filled, clicked


async def probe_one(page, project_key, project_data, wallets):
    url = project_data.get('primary_link') or (project_data.get('custom_checker_links') or [None])[0] or (project_data.get('custom_mint_links') or [None])[0]
    if not url:
        return None

    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    await page.wait_for_timeout(5000)
    body_text = await page.evaluate('() => document.body.innerText')
    # Prefer tweet_author_handle as display name (cleaner than page scraping)
    handle = (project_data.get('tweet_author_handle') or '').strip()
    if handle:
        project_name = handle
    else:
        project_name = infer_project_name(body_text, url)
    chain = infer_chain(body_text, url)
    result = {
        'slug': project_key,
        'name': project_name,
        'link': url,
        'chain': chain,
        'wallets': {},
        'source': 'custom_site',
        'last_seen': None,
    }
    if handle:
        result['tweet_author_handle'] = handle

    for wallet_key, wallet_meta in wallets.items():
        address = wallet_meta['address']
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(3000)
        before = await page.evaluate('() => document.body.innerText')
        _filled, submitted = await try_fill_and_submit(page, address)
        after = await page.evaluate('() => document.body.innerText')
        message = extract_result_message(before, after, submitted)
        result['wallets'][wallet_key] = [message]

    return result


async def main(project_key_filter=None):
    wallets = load_wallet_addresses()
    scraped_path = STATE_DIR / 'scraped_links.json'
    if not scraped_path.exists():
        print('No scraped_links.json found')
        return {}
    with open(scraped_path) as f:
        scraped = json.load(f)
    projects = scraped.get('project_links', {})

    candidate_projects = {
        key: value for key, value in projects.items()
        if value.get('custom_checker_links') or value.get('custom_mint_links')
    }
    if project_key_filter:
        candidate_projects = {k: v for k, v in candidate_projects.items() if k in project_key_filter}

    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context()
        page = await context.new_page()
        for key, value in candidate_projects.items():
            try:
                probed = await probe_one(page, key, value, wallets)
                if probed:
                    results[key] = probed
            except Exception as e:
                results[key] = {
                    'slug': key,
                    'name': infer_project_name('', value.get('primary_link', '')),
                    'link': value.get('primary_link', ''),
                    'chain': 'TBA',
                    'wallets': {wk: ['TBA'] for wk in wallets},
                    'source': 'custom_site',
                    'error': str(e),
                    'last_seen': None,
                }
        await browser.close()

    print(json.dumps(results, indent=2))
    return results


if __name__ == '__main__':
    filter_keys = None
    if len(sys.argv) > 1:
        filter_keys = [x.strip() for x in sys.argv[1].split(',') if x.strip()]
    asyncio.run(main(filter_keys))
