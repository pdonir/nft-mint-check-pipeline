#!/usr/bin/env python3
"""Scrape X Lists for NFT mint/checker links from the last 13 hours."""
import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

WORKLOAD_ROOT = Path(__file__).resolve().parents[2]  # workload root
BASE_DIR = WORKLOAD_ROOT  # alias kept for downstream usages
CONFIG_DIR = WORKLOAD_ROOT / "config"
STATE_DIR = WORKLOAD_ROOT / "state"
COOKIES_FILE = os.environ.get("TWITTER_COOKIES_FILE", "/path/to/twitter_cookies.json")
HOURS_WINDOW = 13
FALLBACK_TTL = timedelta(days=1)
OPENSEA_PUBLIC_GRACE = timedelta(hours=1)
GQL_URL = "https://gql.opensea.io/graphql"
GQL_QUERY = """
query DropPublicStagesQuery($collectionSlug: String!, $address: Address!) {
  dropBySlug(slug: $collectionSlug) {
    __typename
    ... on Erc721SeaDropV1 {
      minterQuantityMinted(minter: $address)
    }
    stages {
      stageType
      startTime
      maxTotalMintableByWallet
      label
      stageIndex
      price {
        token {
          unit
          symbol
        }
      }
    }
  }
}
"""
PUBLIC_STAGE_KEYWORDS = ("public",)
DUMMY_ADDRESS = "0x000000000000000000000000000000000000dEaD"
# Auto-detect local timezone from system
def _detect_local_tz():
    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_env)
        except Exception:
            pass
    offset = datetime.now(timezone.utc).astimezone().utcoffset()
    if offset and offset.total_seconds() != 0:
        return timezone(offset)
    return timezone(timedelta(hours=7))  # fallback

LOCAL_TZ = _detect_local_tz()
LOCAL_TZ_OFFSET = LOCAL_TZ.utcoffset(datetime.now())
LOCAL_TZ_NAME = f"GMT+{int(LOCAL_TZ_OFFSET.total_seconds() // 3600)}" if LOCAL_TZ_OFFSET.total_seconds() >= 0 else f"GMT{int(LOCAL_TZ_OFFSET.total_seconds() // 3600)}"

KNOWN_PLATFORM_PATTERNS = [
    (r'https?://(?:www\.)?opensea\.io/collection/([^/\s"\'\?#]+)', 'opensea', 'opensea_drop'),
    (r'https?://zora\.co/collect/[^/\s"\']+/([^/\s"\'\?#]+)', 'zora', 'custom_mint'),
    (r'https?://mint\.fun/([^/\s"\'\?#]+)', 'mintfun', 'custom_mint'),
    (r'https?://highlight\.xyz/mint/([^/\s"\'\?#]+)', 'highlight', 'custom_mint'),
    (r'https?://manifold\.xyz/c/([^/\s"\'\?#]+)', 'manifold', 'custom_mint'),
    (r'https?://foundation\.app/([^/\s"\'\?#]+)', 'foundation', 'custom_mint'),
    (r'https?://rarible\.com/collection/([^/\s"\'\?#]+)', 'rarible', 'custom_mint'),
    (r'https?://premint\.xyz/([^/\s"\'\?#]+)', 'premint', 'eligibility_checker'),
    (r'https?://alphabot\.app/([^/\s"\'\?#]+)', 'alphabot', 'eligibility_checker'),
]

URL_REGEX = re.compile(r'https?://[^\s<>"\']+')
CHECKER_TWEET_KEYWORDS = [
    'checker', 'check wallet', 'check eligibility', 'eligibility', 'verify wallet', 'wallet checker', 'check your wallet'
]
NON_CHECKER_TWEET_KEYWORDS = [
    'apply whitelist', 'apply wl', 'apply now', 'apply here', 'whitelist application', 'register wl', 'register whitelist', 'join wl', 'wl application'
]
EXCLUDED_HOSTS = {
    'x.com', 'twitter.com', 't.co', 'pbs.twimg.com', 'video.twimg.com',
    'help.x.com', 'help.twitter.com', 'abs.twimg.com'
}


def load_cookies():
    with open(COOKIES_FILE) as f:
        return json.load(f)


def normalize_url(url: str) -> str:
    url = (url or '').strip().rstrip('.,);]\"\'')
    return url


async def resolve_tco(url):
    """Resolve t.co redirect to final URL."""
    if not url or 't.co/' not in url:
        return url
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
            resp = await client.head(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            if resp.status_code in (301, 302, 303, 307, 308):
                return resp.headers.get('location', url)
    except Exception:
        pass
    return url


def classify_generic_url(url: str, tweet_text: str = ''):
    parsed = urlparse(url)
    host = (parsed.netloc or '').lower().replace('www.', '')
    tweet_low = (tweet_text or '').lower()

    if not host or host in EXCLUDED_HOSTS:
        return None

    for pattern, platform, category in KNOWN_PLATFORM_PATTERNS:
        m = re.search(pattern, url, re.IGNORECASE)
        if m:
            slug = m.group(1).rstrip('…/\\. \t\n\r')
            return {
                'url': url,
                'slug': slug,
                'platform': platform,
                'category': category,
                'source_type': 'known_pattern',
            }

    if any(k in tweet_low for k in CHECKER_TWEET_KEYWORDS):
        return {
            'url': url,
            'slug': '',
            'platform': host,
            'category': 'eligibility_checker',
            'source_type': 'tweet_keyword_classifier',
        }

    return None


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def link_identity(item):
    if item.get('platform') == 'opensea' and item.get('slug'):
        return f"opensea:{item['slug'].lower()}"
    return (item.get('url') or '').strip().lower()


def choose_primary_link(project_links):
    priority = ['opensea_drop', 'eligibility_checker', 'custom_mint', 'claim']
    for category in priority:
        links = project_links.get(category) or []
        if links:
            return links[0]
    return ''


async def fetch_opensea_public_metadata(slug: str):
    if not slug:
        return {'metadata_status': 'missing_slug'}
    headers = {
        'content-type': 'application/json',
        'x-app-id': 'os2-web',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/138 Safari/537.36',
        'origin': 'https://opensea.io',
        'referer': f'https://opensea.io/collection/{slug}/overview',
    }
    body = {
        'operationName': 'DropPublicStagesQuery',
        'query': GQL_QUERY,
        'variables': {'collectionSlug': slug, 'address': DUMMY_ADDRESS},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(GQL_URL, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {'metadata_status': 'error', 'metadata_error': f'{type(exc).__name__}: {exc}'}

    drop = (data.get('data') or {}).get('dropBySlug')
    if not drop:
        return {'metadata_status': 'no_drop', 'metadata_error': 'dropBySlug returned no data'}

    stages = drop.get('stages') or []
    public_stages = []
    for stage in stages:
        label = (stage.get('label') or '').lower()
        stage_type = (stage.get('stageType') or '').upper()
        if stage_type == 'PUBLIC_SALE' or any(k in label for k in PUBLIC_STAGE_KEYWORDS):
            public_stages.append(stage)
    public_stages.sort(key=lambda s: s.get('startTime') or '')
    public_stage = public_stages[0] if public_stages else None
    public_start = parse_iso(public_stage.get('startTime')) if public_stage else None
    meta = {
        'metadata_status': 'ok',
        'metadata_error': '',
        'opensea_stage_count': len(stages),
        'public_start_time': iso(public_start),
    }
    if public_stage:
        meta['public_stage'] = {
            'label': public_stage.get('label') or f"Stage {public_stage.get('stageIndex')}",
            'stage_type': public_stage.get('stageType') or '',
            'start_time': iso(public_start),
            'max_total_mintable_by_wallet': public_stage.get('maxTotalMintableByWallet'),
            'price': public_stage.get('price') or {},
        }
    else:
        meta['metadata_status'] = 'no_public_stage'
        meta['metadata_error'] = 'No public stage found yet'
    return meta


async def enrich_opensea_metadata(links):
    opensea_items = [item for item in links if item.get('platform') == 'opensea' and item.get('slug')]
    if not opensea_items:
        return
    print(f"[*] Fetching OpenSea public stage metadata for {len(opensea_items)} links...")
    for item in opensea_items:
        meta = await fetch_opensea_public_metadata(item.get('slug'))
        item.update(meta)
        status = meta.get('metadata_status')
        public_start = meta.get('public_start_time') or 'TBA'
        print(f"    {item.get('slug')}: {status}, public_start={public_start}")


def apply_expiry(item, now):
    public_start = parse_iso(item.get('public_start_time'))
    if item.get('platform') == 'opensea' and public_start:
        expires = public_start + OPENSEA_PUBLIC_GRACE
        item['cleanup_rule'] = 'opensea_public_start_plus_1h'
    else:
        last_seen = parse_iso(item.get('last_seen')) or parse_iso(item.get('first_seen')) or now
        expires = last_seen + FALLBACK_TTL
        item['cleanup_rule'] = 'fallback_ttl_3d'
    item['expires_at'] = iso(expires)
    item['active'] = now <= expires
    return item


def merge_scraped_links(existing_output, new_links, now):
    existing_links = existing_output.get('all_links') or [] if isinstance(existing_output, dict) else []
    merged = {}

    for old in existing_links:
        if not isinstance(old, dict) or not old.get('url'):
            continue
        key = link_identity(old)
        old.setdefault('first_seen', old.get('tweet_time') or existing_output.get('scrape_time') or iso(now))
        old.setdefault('last_seen', old.get('tweet_time') or existing_output.get('scrape_time') or iso(now))
        merged[key] = old

    for new in new_links:
        key = link_identity(new)
        old = merged.get(key, {})
        first_seen = old.get('first_seen') or new.get('tweet_time') or iso(now)
        combined = dict(old)
        combined.update({k: v for k, v in new.items() if v not in (None, '', [])})
        combined['first_seen'] = first_seen
        combined['last_seen'] = iso(now)
        combined['last_scraped_at'] = iso(now)
        if old.get('source_tweets') and new.get('tweet_url'):
            tweets = list(dict.fromkeys(old.get('source_tweets', []) + [new.get('tweet_url')]))
            combined['source_tweets'] = tweets
        merged[key] = combined

    kept = []
    removed = []
    for item in merged.values():
        apply_expiry(item, now)
        if item.get('active'):
            kept.append(item)
        else:
            removed.append(item)

    kept.sort(key=lambda x: x.get('last_seen') or '', reverse=True)
    return kept, removed


def load_existing_scraped(output_file):
    if not os.path.exists(output_file):
        return {}
    try:
        with open(output_file) as f:
            return json.load(f)
    except Exception:
        return {}


def build_project_buckets(unique_links):
    projects = {}
    handle_to_key = {}

    for item in unique_links:
        handle = (item.get('tweet_author_handle') or '').lower().strip()
        raw_key = item.get('project_key') or item.get('slug') or urlparse(item['url']).netloc.lower()
        key = handle_to_key.get(handle) if handle else None
        if not key:
            key = raw_key
        proj = projects.setdefault(key, {
            'project_key': key,
            'opensea_link': '',
            'custom_checker_links': [],
            'custom_mint_links': [],
            'claim_links': [],
            'all_links': [],
            'primary_link': '',
            'source_tweets': [],
            'tweet_author_handle': item.get('tweet_author_handle', ''),
        })
        if handle:
            handle_to_key[handle] = key

        url = item['url']
        if url not in proj['all_links']:
            proj['all_links'].append(url)
        tweet_url = item.get('tweet_url') or ''
        if tweet_url and tweet_url not in proj['source_tweets']:
            proj['source_tweets'].append(tweet_url)
        if item.get('tweet_author_handle') and not proj.get('tweet_author_handle'):
            proj['tweet_author_handle'] = item.get('tweet_author_handle')

        cat = item.get('category', 'unknown_mint_related')
        if cat == 'opensea_drop' and not proj['opensea_link']:
            proj['opensea_link'] = url
        elif cat == 'eligibility_checker' and url not in proj['custom_checker_links']:
            proj['custom_checker_links'].append(url)
        elif cat == 'custom_mint' and url not in proj['custom_mint_links']:
            proj['custom_mint_links'].append(url)
        elif cat == 'claim' and url not in proj['claim_links']:
            proj['claim_links'].append(url)

    for proj in projects.values():
        proj['primary_link'] = choose_primary_link({
            'opensea_drop': [proj['opensea_link']] if proj['opensea_link'] else [],
            'eligibility_checker': proj['custom_checker_links'],
            'custom_mint': proj['custom_mint_links'],
            'claim': proj['claim_links'],
        })

    return projects


async def scrape_list(page, list_url, cutoff_time):
    """Scrape a single X List page for mint/checker links."""
    print(f"  scraping {list_url}...")
    await page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)

    body_text = await page.evaluate("() => document.body.innerText")
    if "Sign in" in body_text[:500]:
        print("    [!] NOT LOGGED IN , cookies may be expired")
        return []

    mint_links = []
    seen_urls = set()
    max_scrolls = 30
    consecutive_old = 0

    for _ in range(max_scrolls):
        tweets = await page.query_selector_all('article[data-testid="tweet"]')
        found_any_new = False

        for tweet in tweets:
            time_el = await tweet.query_selector('time')
            if not time_el:
                continue
            tweet_time = await time_el.get_attribute('datetime')
            if not tweet_time:
                continue

            tweet_dt = datetime.fromisoformat(tweet_time.replace('Z', '+00:00'))
            if tweet_dt < cutoff_time:
                continue

            found_any_new = True
            tweet_text = await tweet.evaluate("el => el.innerText")
            tweet_text_clean = tweet_text.replace('\n', ' ')
            tweet_url = ''
            tweet_author_handle = ''
            try:
                tweet_url = await time_el.evaluate("el => el.closest('a') ? el.closest('a').href : ''")
            except Exception:
                pass
            if tweet_url and '/status/' in tweet_url:
                try:
                    tweet_author_handle = tweet_url.split('x.com/')[1].split('/status/')[0].strip()
                except Exception:
                    tweet_author_handle = ''

            link_data = await tweet.eval_on_selector_all(
                'a[href]', 'els => els.map(e => ({text: e.innerText, href: e.href}))'
            )

            all_link_texts = []
            all_hrefs = []
            for ld in link_data:
                if ld.get('text'):
                    all_link_texts.append(ld['text'].replace('\n', ' '))
                if ld.get('href'):
                    href = normalize_url(ld['href'])
                    if href:
                        all_hrefs.append(href)

            resolved_hrefs = []
            for url in all_hrefs:
                if 't.co/' in url:
                    resolved_hrefs.append(normalize_url(await resolve_tco(url)))
                else:
                    resolved_hrefs.append(url)

            combined_text = tweet_text_clean + ' ' + ' '.join(all_link_texts) + ' ' + ' '.join(resolved_hrefs)
            text_urls = [normalize_url(m.group(0)) for m in URL_REGEX.finditer(combined_text)]
            candidate_urls = []
            for u in resolved_hrefs + text_urls:
                u = normalize_url(u)
                if u and u not in candidate_urls:
                    candidate_urls.append(u)

            for url in candidate_urls:
                classified = classify_generic_url(url, tweet_text_clean)
                if not classified:
                    continue
                if classified['url'] in seen_urls:
                    continue
                seen_urls.add(classified['url'])
                classified['tweet_time'] = tweet_time
                classified['tweet_url'] = tweet_url
                classified['tweet_text'] = tweet_text_clean
                classified['tweet_author_handle'] = tweet_author_handle
                classified['list_url'] = list_url
                project_key = classified.get('slug') or urlparse(classified['url']).netloc.lower().replace('www.', '')
                classified['project_key'] = project_key
                mint_links.append(classified)

        if not found_any_new:
            consecutive_old += 1
            if consecutive_old >= 3:
                break
        else:
            consecutive_old = 0

        await page.evaluate("window.scrollBy(0, 2000)")
        await page.wait_for_timeout(2000)

    return mint_links


async def main():
    list_urls_file = CONFIG_DIR / "list_nft.json"
    with open(list_urls_file) as f:
        list_urls = json.load(f)

    if not list_urls:
        print("No list URLs in list_nft.json")
        return

    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(hours=HOURS_WINDOW)
    cutoff_local = cutoff_time.astimezone(LOCAL_TZ)
    print(f"[*] Scraping X Lists for mints since {cutoff_local.strftime('%d %b %H:%M')} {LOCAL_TZ_NAME}")

    cookies = load_cookies()
    all_links = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)
        page = await context.new_page()

        for list_url in list_urls:
            links = await scrape_list(page, list_url, cutoff_time)
            all_links.extend(links)
            print(f"    found {len(links)} mint/checker links")

        await browser.close()

    seen = set()
    unique_links = []
    for link in all_links:
        if link['url'] not in seen:
            seen.add(link['url'])
            unique_links.append(link)

    await enrich_opensea_metadata(unique_links)

    output_file = STATE_DIR / "scraped_links.json"
    existing_output = load_existing_scraped(output_file)
    merged_links, removed_links = merge_scraped_links(existing_output, unique_links, now)

    opensea_links = [l for l in merged_links if l.get('category') == 'opensea_drop']
    project_links = build_project_buckets(merged_links)
    category_counts = {}
    for item in merged_links:
        category_counts[item.get('category', 'unknown')] = category_counts.get(item.get('category', 'unknown'), 0) + 1

    print(f"\n[*] New unique mint/checker links this scrape: {len(unique_links)}")
    print(f"[*] Active cached mint/checker links: {len(merged_links)}")
    print(f"[*] Removed expired cached links: {len(removed_links)}")
    print(f"[*] Active OpenSea links: {len(opensea_links)}")
    print(f"[*] Active project buckets: {len(project_links)}")
    print(f"[*] Active category counts: {category_counts}")

    output = {
        'scrape_time': now.isoformat(),
        'cutoff_time': cutoff_time.isoformat(),
        'cache_policy': {
            'opensea': 'expires at public_start_time + 1h; fallback last_seen + 1d when public start unavailable',
            'non_opensea': 'expires at last_seen + 1d',
        },
        'all_links': merged_links,
        'removed_links': removed_links,
        'opensea_slugs': [l['slug'] for l in opensea_links if l.get('slug')],
        'project_links': project_links,
        'category_counts': category_counts,
    }

    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"[*] Saved to {output_file}")
    return output


if __name__ == "__main__":
    asyncio.run(main())
