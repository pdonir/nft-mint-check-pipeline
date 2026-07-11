# Secret scanner for nft-trade/

Automated detection of accidentally-committed secrets. Use as a pre-commit
hook or run standalone.

## What it detects

- **Ethereum private keys** (`0x` + 64 hex chars)
- **Alchemy RPC URLs** with embedded API key
- **Twitter cookies** (`auth_token`, `ct0`)
- **GitHub PATs** (`github_pat_*`, `ghp_*`, `gho_*`)
- **Generic high-entropy strings** (heuristic, Shannon entropy)

## Allowlist (built-in)

Known-safe public constants are automatically excluded:
- `CONDUIT_KEY`, `OPENSEA_FEE_RECIPIENT`, `OPENSEA_ZONE`, `SEAPORT_*`
- Exact public values like `0x0000a26b00c1f0df...` (OpenSea fee recipient)

Lines starting with `# example`, `# e.g.`, `# placeholder`, or `# 0x12...` are
treated as intentional placeholders.

## Skip paths

These paths are NEVER scanned (they legitimately contain secrets and should
be in `.gitignore`):
- `shared/wallets/` — wallet config (private keys)
- `shared/secrets/` — env files (API keys, passwords)
- `.git/`, `__pycache__/`, `.venv/`, `node_modules/`, `*.pyc`

## Usage

```bash
# Standalone — scan everything
python3 scripts/secret_scan.py

# Pre-commit — scan only staged files
python3 scripts/secret_scan.py --staged

# Strict mode — treat warnings as errors
python3 scripts/secret_scan.py --strict

# Scan specific file
python3 scripts/secret_scan.py path/to/file.py
```

## Install as git hook

After `git init` in `/home/Donir/NFT/nft-trade/`:

```bash
# Option A: direct copy
cp scripts/git-hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit

# Option B: use scripts/git-hooks/ as the hooks directory (version-controlled)
git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/pre-commit
```

Option B is preferred — hooks stay version-controlled.

## Bypass for false positives

```bash
git commit --no-verify -m "..."
```

But first add the false-positive pattern to `SAFE_VARIABLE_PATTERNS` or
`SAFE_EXACT_VALUES` in `secret_scan.py`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Clean — no issues |
| 1 | Critical issue(s) found |
| 2 | Warning(s) found in `--strict` mode |
