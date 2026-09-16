#!/bin/bash
# Run NFT WL Check pipeline. The Python pipeline sends Telegram reports itself.
# Success stays silent for systemd / cron; failures print a short alert.
#
# Paths are derived from the script's own location — no hardcoded absolute paths,
# so this works from any clone dir.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKLOAD_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$WORKLOAD_ROOT"

export TZ='Asia/Jakarta'
ENV_FILE="$WORKLOAD_ROOT/config/upcoming_mints_notifier.env"
SHARED_ENV_FILE="${SHARED_ENV_FILE:-${WORKLOAD_ROOT}/../shared/secrets.env}"
# After the 2026-07-11 secrets reorg, the monolithic secrets.env was split into
# per-secret files under shared/secrets/. The legacy path may no longer exist;
# fall back to loading each file so systemd/manual runs both work.
SHARED_SECRETS_DIR="${WORKLOAD_ROOT}/../shared/secrets"

# Helper: source a file line-by-line as KEY=value pairs. Skips blanks/comments.
load_env_file() {
  local file="$1"
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key=${line%%=*}
    value=${line#*=}
    value=${value%\"}
    value=${value#\"}
    value=${value%\'}
    value=${value#\'}
    export "$key=$value"
  done < "$file"
}

# Load shared secrets first (single source of truth for ALCHEMY_API_KEY),
# then workload env (workload values can override shared defaults).
if [ -f "$SHARED_ENV_FILE" ]; then
  load_env_file "$SHARED_ENV_FILE"
else
  # Legacy monolith missing — source each per-secret file in shared/secrets/.
  # - Files containing '=' are KEY=value (e.g. alchemy.env).
  # - Bare-value files (e.g. opensea_api_key) get treated as ALCHEMY_API_KEY
  #   only if the variable is still unset, to stay backward-compatible.
  if [ -d "$SHARED_SECRETS_DIR" ]; then
    for _f in "$SHARED_SECRETS_DIR"/*.env; do
      [ -f "$_f" ] || continue
      load_env_file "$_f"
    done
    if [ -z "${ALCHEMY_API_KEY:-}" ] && [ -f "$SHARED_SECRETS_DIR/opensea_api_key" ]; then
      export ALCHEMY_API_KEY="$(cat "$SHARED_SECRETS_DIR/opensea_api_key")"
    fi
  fi
  # If still missing, allow systemd EnvironmentFile (or shell session) to provide it.
fi
load_env_file "$ENV_FILE"

# `config/wallets.json` is the operator-controlled allowlist. Validate each
# selected alias/address against the private canonical config before spawning.
python3 "$WORKLOAD_ROOT/scripts/validate_wallet_allowlist.py" >/dev/null

LOCK=${NFT_MINT_CHECK_LOCK_FILE:-/tmp/nft_mint_check.lock}
PIDFILE=${NFT_MINT_CHECK_PIDFILE:-/tmp/nft_mint_check.pid}
SERVICE_LOCK=${NFT_SERVICE_LOCK_FILE:-/tmp/nft_service.lock}
LOG="$WORKLOAD_ROOT/logs/last_run.log"

if [ -f "$PIDFILE" ]; then
  OLDPID=$(cat "$PIDFILE" 2>/dev/null || true)
  if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
    exit 0
  fi
  rm -f "$PIDFILE"
fi

nohup setsid bash -c "
  cd '$WORKLOAD_ROOT'
  # Service gate first — must be free so we don't race /runcheck /getlinks /check
  # which all read+write upcoming_mints.json / scraped_links.json.
  exec 9>'$SERVICE_LOCK'
  if ! flock -n 9; then
    echo 'service gate busy (another bot command or pipeline running), skipping'
    exit 0
  fi
  # Per-pipeline lock — protects against duplicate cron firings.
  exec 8>'$LOCK'
  if ! flock -n 8; then
    echo 'pipeline lock held, skipping'
    exit 0
  fi
  echo \"pid=\$\$ started=\$(date '+%Y-%m-%d %H:%M:%S %Z') source=cron-detached\"
  timeout 900 python3 -u scripts/pipeline/nft_wl_check_pipeline.py
  ec=\$?
  echo \"=== Run ended: \$(date '+%Y-%m-%d %H:%M:%S %Z') (exit \$ec) ===\"
" > "$LOG" 2>&1 &

NEWPID=$!
disown 2>/dev/null || true
echo "$NEWPID" > "$PIDFILE"
exit 0
