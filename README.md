# NFT Mint Check Pipeline

Automated NFT mint tracking pipeline — scrapes Twitter/X Lists for newly announced
mint links and checker sites, checks eligibility across all configured wallets, and
sends scheduled Telegram reports.

**Main use case:** the moment a project you follow announces a link checker on
X, this pipeline (a) picks it up from your curated X Lists, (b) checks eligibility
for every wallet you own against the appropriate backend (OpenSea GraphQL or the
project's custom site), and (c) posts a per-project report to your Telegram
"Upcoming Mints" topic — including price, mint cap, and the exact local-time start
of every stage (GTD / WL / FCFS / Public).

A Telegram bot is included so you can re-check any project on demand from your
phone (`/check`, `/links`), query the current state (`/upcoming`, `/today`,
`/slug`), or trigger the full pipeline (`/runcheck`) without SSH'ing into the
server.

---

## What It Does

The full pipeline runs in five steps:

1. **Scrape X Lists** — Playwright + httpx pulls tweets from your curated X Lists
   within a configurable hour window. Detects OpenSea drops and external checker
   sites (onchainchecker.xyz, project-owned ELIGIBILITY pages, etc.).
2. **Cleanup** — Removes expired entries (default: 1h after the last tier ends).
3. **Eligibility Check** — Two parallel paths:
   - **OpenSea drops:** SIWE login + GraphQL query (`dropBySlug`) — no browser
     required, fast (~3s per wallet).
   - **Custom checker sites:** Playwright scrapes per project.
4. **Persist state** — Writes per-project eligibility + stage timing to
   `state/upcoming_mints.json` and per-tweet scraped links to
   `state/scraped_links.json`.
5. **Report** — Sends two Telegram messages:
   - **Upcoming Mints** — every active project (sorted by earliest stage time).
   - **Mint Today** — subset where any stage starts today in your local TZ.

Long messages are auto-split at paragraph boundaries (Telegram 4096-char limit).

---

## Files

| Path | Description |
|------|-------------|
| `scripts/pipeline/nft_wl_check_pipeline.py` | Main pipeline — orchestrates scrape → eligibility → report |
| `scripts/bot/upcoming_mints_tg_bot.py` | Telegram command bot — handles all `/` commands |
| `scripts/scraper/x_list_scraper.py` | Twitter/X List scraper (Playwright + httpx), persists scraped links |
| `scripts/checker/custom_site_checker.py` | Generic checker for non-OpenSea eligibility sites (Playwright) |
| `scripts/checker/opensea_checker/opensea_checker_api.py` | OpenSea eligibility — **API-only** (single wallet × single slug) |
| `scripts/checker/opensea_checker/opensea_checker_api_batch.py` | OpenSea eligibility — **API-only** (multi wallet × multi slug) |
| `scripts/checker/opensea_checker/opensea_checker_browser.py` | OpenSea eligibility — **browser fallback** (single wallet, debug/QA) |
| `scripts/checker/opensea_checker/opensea_checker_browser_batch.py` | OpenSea eligibility — **browser fallback** (multi wallet × multi slug) |
| `scripts/checker/opensea_checker/siwe_login.py` | SIWE auth helper shared by API scripts |
| `scripts/run_nft_check.sh` | Cron/timer wrapper — loads env, locks, spawns pipeline detached |
| `config/wallets.example.json` | Wallet display labels (key → display name) |
| `config/list_nft.example.json` | X List URLs to scrape |

---

## Prerequisites

| Dependency | Required For | Install |
|-----------|-------------|---------|
| Python 3.10+ | Everything | `apt install python3` |
| Playwright + Chromium | X scraping + custom checker + browser-based OpenSea fallback | `pip install playwright && playwright install chromium` |
| httpx | t.co link resolution | `pip install httpx` |
| requests, eth-account | OpenSea SIWE + GraphQL checker | `pip install requests eth-account` |
| xvfb | Headless browser on VPS (no display) | `apt install xvfb` |
| Telegram bot | Receiving reports + sending commands | Create via [@BotFather](https://t.me/BotFather) |

---

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
cp config/wallets.example.json config/wallets.json
cp config/list_nft.example.json config/list_nft.json
```

**`config/wallets.json`** — wallet keys plus display labels. Keys MUST match the
keys in your wallet credentials file:

```json
{
  "wallet_1": { "display": "Wallet 1" },
  "wallet_2": { "display": "Wallet 2" }
}
```

**`config/list_nft.json`** — X List URLs to scrape:

```json
[
  "https://x.com/i/lists/YOUR_LIST_ID_1",
  "https://x.com/i/lists/YOUR_LIST_ID_2"
]
```

**`nft_config.json`** (typically in the parent workload directory, e.g.
`../shared/wallets/nft_config.json`) — wallet credentials for OpenSea SIWE
eligibility checks. The path is taken from `$NFT_CONFIG_PATH`:

```json
{
  "wallets": {
    "wallet_1": { "address": "0xYOUR_WALLET_ADDRESS_1", "private_key": "YOUR_PRIVATE_KEY_1" },
    "wallet_2": { "address": "0xYOUR_WALLET_ADDRESS_2", "private_key": "YOUR_PRIVATE_KEY_2" }
  }
}
```

Keep `nft_config.json` outside this repo (e.g. in a shared `secrets/` directory).
Never commit wallet addresses, private keys, cookies, real X List IDs, or
generated scrape/report files.

### 3. Export Twitter cookies

Export cookies from your browser (logged into X/Twitter) and save as
`twitter_cookies.json` in your home directory (path taken from
`$TWITTER_COOKIES_FILE`). Format: standard Playwright cookie export array.
You can export using browser extensions like "Cookie Editor" or Playwright's
`context.cookies()`.

### 4. Set environment variables

The simplest path is to use the systemd env file:

```bash
sudo cp config/upcoming_mints_notifier.env.example /etc/systemd/user/nft-wl-check.env
sudo systemctl daemon-reload
sudo systemctl edit --user upcoming-mints-notifier.service
# → add EnvironmentFile=/etc/systemd/user/nft-wl-check.env
```

Or export inline per-command:

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export UPCOMING_MINTS_THREAD_ID="100"            # optional: specific topic id

# Optional: override system timezone (default: auto-detect, fallback: GMT+7)
export TZ="Asia/Jakarta"
```

### 5. (Optional) Set up the Rabby password

The browser-based OpenSea scripts unlock a local Rabby extension. The password
must be supplied via either:

- `export RABBY_PASSWORD='your_password'` — env var wins, or
- `$RABBY_SECRETS_FILE` (default `/path/to/rabby_password.env`, chmod 600) — fallback secrets file:

  ```
  RABBY_PASSWORD='your_password'
  ```

The script aborts with a clear error if neither source is set.

#### 5a. (Optional) Browser script paths

The browser scripts also need a Rabby extension folder and a per-wallet profile
dir. Both default to generic placeholders — set these env vars to point at your
real copies:

```bash
export RABBY_EXTENSION_DIR="/path/to/rabby_extension"           # unpacked Rabby extension folder
export OPENSEA_PROFILE_DIR="/path/to/browser_profiles/opensea"   # per-instance browser profile
```

If you skip these, the scripts will fail at launch with a clear `Path does not
exist` error.

### 6. Run

Manual run (foreground):

```bash
python3 scripts/pipeline/nft_wl_check_pipeline.py
```

On a headless VPS:

```bash
xvfb-run -a python3 scripts/pipeline/nft_wl_check_pipeline.py
```

The systemd-managed approach (recommended for unattended scheduling) lives in
`scripts/run_nft_check.sh`. It loads the env file, takes an exclusive lock, and
spawns the pipeline detached so multiple triggers don't overlap.

---

## Telegram Bot Commands

The bot (`scripts/bot/upcoming_mints_tg_bot.py`) is driven by long-polling, no
webhook. Send `/start` to your bot once before first use.

| Command | Description | Example |
|---------|-------------|---------|
| `/start`, `/help` | Show help + command list | `/start` |
| `/upcoming <wallet>` | All active projects tracked for a wallet | `/upcoming Wallet 1` |
| `/today <wallet>` | Subset whose any stage starts today (local TZ) | `/today Wallet 1` |
| `/slug <name> [—wallet]` | Detailed eligibility for one project (fuzzy match) | `/slug example-slug`, `/slug example-slug —Wallet 1` |
| `/links` | All project entries from `upcoming_mints.json` as numbered `[name](checker_url)` markdown links, blank line every 5 | `/links` |
| `/getlinks` | **Admin only:** run the X-list scraper in the background (no eligibility check). Bot replies when done/failed. | `/getlinks` |
| `/check <slug> [—wallet]` | Re-run eligibility for one slug; auto-fuzzy typo; merges into `upcoming_mints.json` | `/check example-slug`, `/check example-slug —Wallet 1` |
| `/runcheck` | **Admin only:** trigger the full pipeline (scrape + check) | `/runcheck` |
| `/runcheck status` | Admin: show pipeline idle/running | `/runcheck status` |

**`/links` vs `/getlinks`:** `/links` is a *read* command — it shows whatever is
currently in `upcoming_mints.json`. `/getlinks` is a *write* command — it triggers
the scraper to fetch new links from X lists in the background. Use `/getlinks`
when you want fresh data, then `/links` to see what got picked up. `/runcheck`
does both in one shot (scrape + eligibility check across all wallets).

**Mutual-exclusion lock:** `/runcheck`, `/getlinks`, `/check`, and the cron
pipeline (via `scripts/run_nft_check.sh`) all take a shared service-gate lock
(`/tmp/nft_service.lock` by default, override via `NFT_SERVICE_LOCK_FILE`).
Only one may run at a time — the others get a "Service lain masih jalan"
message and must wait. This prevents `upcoming_mints.json` from being
read/written concurrently by overlapping runs (especially important since
`/runcheck` and `/getlinks` run in background threads while `/check` runs
synchronously in the main loop, and the cron pipeline runs detached).

The cron wrapper acquires the gate **first** (non-blocking `flock -n`), then
its own per-pipeline lock (`/tmp/nft_mint_check.lock`). If the gate is busy,
the wrapper logs `service gate busy, skipping` and exits silently — the
systemd timer unit reports success so no alert spam.

**Wallet aliases accepted by all commands:** display name (`Wallet 1`), the key
(`wallet_1`), or any prefix thereof (case-insensitive). The em-dash form
(`—Wallet 1`) is shorthand for `--wallet Wallet 1` in `/slug` and `/check`.

**Auto-typo correction:** `/slug` and `/check` use the same `difflib`-based fuzzy
matcher. Typing `/check exampl-slug` (one letter off) auto-corrects to
`example-slug` silently; closer-to-novel strings get an explicit "_Fuzzy match for
…_ (score …)" prefix so you know a guess happened.

**Thread routing:** by default commands must be sent in the configured
`UPCOMING_MINTS_THREAD_ID` topic. Multi-topic setups can override via
`UPCOMING_MINTS_ALLOWED_THREAD_IDS` (comma-separated).

---

## Scheduling

### Systemd timer (recommended, current setup)

Two daily firings at 00:00 and 12:00 UTC (= 07:00 and 19:00 GMT+7 if your TZ is
`Asia/Jakarta`):

```bash
systemctl --user daemon-reload
systemctl --user enable --now nft-wl-check-pipeline.timer
systemctl --user list-timers nft-wl-check-pipeline.timer
```

Unit files: `~/.config/systemd/user/nft-wl-check-pipeline.service` and
`~/.config/systemd/user/nft-wl-check-pipeline.timer`. `Persistent=true` so
missed firings (e.g. after a reboot) catch up.

### Crontab fallback

For hosts without systemd user sessions, the legacy cron expression is equivalent:

```cron
0 0,12 * * * cd /path/to/nft-wl-check && /path/to/nft-wl-check/scripts/run_nft_check.sh
```

The wrapper takes the same exclusive lock and detaches the pipeline the same way
the systemd timer does, so both paths are race-safe against each other and against
`/runcheck`.

---

## Pipeline Behavior (what happens when something is missing)

The pipeline handles missing dependencies gracefully — no step crashes the whole
run.

| Missing | Behavior |
|---------|----------|
| `twitter_cookies.json` | Scraper skipped, logs warning |
| `scraped_links.json` (no prior scrape) | Created after first successful scrape |
| `nft_config.json` | Wallet loading fails, eligibility check skipped |
| `xvfb` on headless VPS | X/custom browser scraping may fail; OpenSea API checker still runs without browser |
| Telegram env vars | Reports printed to stdout only, not sent |
| `opensea_checker/` deps | OpenSea eligibility check skipped or logged to `opensea_api_errors.log` |
| `RABBY_PASSWORD` (browser scripts only) | Script aborts with explicit env/secrets hint |

Each step logs its status — look for `[*]`, `[+]`, `[!]` prefixes:
- `[*]` = info/progress
- `[+]` = success
- `[!]` = warning/error (usually non-fatal)

---

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

**"RABBY_PASSWORD not set"**
- Either export `RABBY_PASSWORD` or create `$RABBY_SECRETS_FILE`
  (default `/path/to/rabby_password.env`, chmod 600, format `RABBY_PASSWORD='…'`)

**Timezone showing wrong**
- Set `TZ` environment variable (e.g., `TZ='Asia/Jakarta'`)
- Without `TZ`, auto-detects from system timezone, falls back to GMT+7

**Pipeline not running on schedule**
- Check `systemctl --user status nft-wl-check-pipeline.timer`
- Check `journalctl --user -u nft-wl-check-pipeline.service --since today`
- The pipeline logs go to `logs/last_run.log`; bot logs to
  `logs/upcoming_mints_notifier.log`

---

## Requirements

- Python 3.10+
- Playwright (with Chromium)
- requests + eth-account (for non-browser OpenSea checks)
- xvfb (for headless X scraping / custom browser checks on VPS)
- Twitter/X account with cookies
- Telegram bot token + chat ID
- (Optional) Rabby extension + password for browser-based OpenSea fallback
