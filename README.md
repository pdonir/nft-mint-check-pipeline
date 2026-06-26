# NFT Mint Check Pipeline

Automated NFT mint tracking pipeline — scrapes Twitter/X Lists for mint links, checks eligibility across wallets, and sends Telegram reports.

## What It Does

1. **Scrape X Lists** — Playwright scrapes your X Lists for NFT mint/checker links (configurable hour window)
2. **Cleanup** — Removes expired entries (1h after last tier ends)
3. **Eligibility Check** — Checks OpenSea drops via SIWE + GraphQL (no browser) and custom checker sites across multiple wallets
4. **Report** — Sends `Upcoming Mints` + `Mint Today` to Telegram (auto-splits long messages)

## Files

| File | Description |
|------|-------------|
| `nft_mint_check.py` | Main pipeline — orchestrates all steps |
| `x_list_scraper.py` | Twitter/X List scraper (Playwright + httpx), persists previously scraped links |
| `check_custom_eligibility.py` | Generic checker for non-OpenSea eligibility sites |
| `opensea_checker/` | Non-browser OpenSea SIWE + GraphQL eligibility checker |
| `wallets.example.json` | Wallet config template |
| `list_nft.example.json` | X List URLs template |

## Prerequisites

| Dependency | Required For | Install |
|-----------|-------------|---------|
| Python 3.10+ | Everything | `apt install python3` |
| Playwright + Chromium | X scraping + custom checker fallback | `pip install playwright && playwright install chromium` |
| httpx | t.co link resolution | `pip install httpx` |
| requests, eth-account | OpenSea SIWE + GraphQL checker | `pip install requests eth-account` |
| xvfb | Headless browser on VPS (no display) | `apt install xvfb` |
| Telegram bot | Receiving reports | Create via [@BotFather](https://t.me/BotFather) |

## Setup

### 1. Install dependencies

```bash
pip install playwright httpx requests eth-account
playwright install chromium

# On VPS (headless, no display):
apt install xvfb
```

### 2. Create config files

Copy the example templates and fill in your values:

```bash
cp wallets.example.json wallets.json
cp list_nft.example.json list_nft.json
```

**`wallets.json`** — wallet keys plus display labels (must match keys in `nft_config.json`):
```json
{
  "wallet_1": {
    "display": "Wallet 1"
  },
  "wallet_2": {
    "display": "Wallet 2"
  }
}
```

**`list_nft.json`** — your X List URLs:
```json
[
  "https://x.com/i/lists/YOUR_LIST_ID_1",
  "https://x.com/i/lists/YOUR_LIST_ID_2"
]
```

**`nft_config.json`** (in parent directory) — wallet credentials for OpenSea SIWE eligibility checks:
```json
{
  "wallets": {
    "wallet_1": {
      "address": "0xYOUR_WALLET_ADDRESS_1",
      "private_key": "YOUR_PRIVATE_KEY_1"
    },
    "wallet_2": {
      "address": "0xYOUR_WALLET_ADDRESS_2",
      "private_key": "YOUR_PRIVATE_KEY_2"
    }
  }
}
```

Keep `nft_config.json` outside this repo (default: parent directory). Never commit wallet addresses, private keys, cookies, real X List IDs, or generated scrape/report files.

### 3. Export Twitter cookies

Export cookies from your browser (logged into X/Twitter) and save as `twitter_cookies.json` in the parent directory.

Format: standard Playwright cookie export array. You can export using browser extensions like "Cookie Editor" or Playwright's `context.cookies()`.

### 4. Set environment variables

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"

# Optional: override system timezone (default: auto-detect, fallback: GMT+7)
export TZ="Asia/Jakarta"
```

### 5. Run

```bash
python3 nft_mint_check.py
```

On VPS without display:
```bash
xvfb-run -a python3 nft_mint_check.py
```

## Cron Setup

```bash
# Run at 00:00 and 12:00 UTC (adjust for your timezone)
0 0,12 * * * cd /path/to/nft_cron && TZ='Asia/Jakarta' TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python3 nft_mint_check.py 2>&1 >> /tmp/nft_cron.log
```

## Pipeline Behavior (what happens when something is missing)

The pipeline handles missing dependencies gracefully:

| Missing | Behavior |
|---------|----------|
| `twitter_cookies.json` | Scraper skipped, logs warning |
| `scraped_links.json` (no prior scrape) | Created after first successful scrape |
| `nft_config.json` | Wallet loading fails, eligibility check skipped |
| `xvfb` on headless VPS | X/custom browser scraping may fail; OpenSea API checker still runs without browser |
| Telegram env vars | Reports printed to stdout only, not sent |
| `opensea_checker/` deps | OpenSea eligibility check skipped or logged to `opensea_api_errors.log` |

Each step logs its status — check the output for `[*]`, `[+]`, `[!]` prefixes:
- `[*]` = info/progress
- `[+]` = success
- `[!]` = warning/error (usually non-fatal)

## Troubleshooting

**"Scraper timed out"**
- Twitter cookies may be expired — re-export from browser
- X may have changed their DOM structure — check Playwright selectors

**"Eligibility check failed"**
- Ensure `requests` and `eth-account` are installed
- Check that `nft_config.json` contains wallet keys matching `wallets.json`
- OpenSea API/SIWE may be rate-limited or temporarily changed; check `opensea_api_errors.log`
- Custom checker sites may be down or changed

**"Telegram send failed"**
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are correct
- Bot must be started by the user (send `/start` to the bot)

**Timezone showing wrong**
- Set `TZ` environment variable (e.g., `TZ='Asia/Jakarta'`)
- Without `TZ`, auto-detects from system timezone, falls back to GMT+7

## Requirements

- Python 3.10+
- Playwright (with Chromium)
- requests + eth-account (for non-browser OpenSea checks)
- xvfb (for headless X scraping/custom browser checks on VPS)
- Twitter/X account with cookies
- Telegram bot token + chat ID
