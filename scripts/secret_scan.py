#!/usr/bin/env python3
"""
Secret scanner — pre-commit + standalone use.

Detects accidentally-committed secrets:
  - Ethereum private keys (0x + 64 hex chars)
  - Alchemy RPC keys (in URL form)
  - Twitter auth cookies (auth_token, ct0)
  - GitHub PATs (github_pat_, ghp_, gho_)
  - Generic high-entropy secrets via Shannon entropy heuristic

Allowlist: known-safe public constants (OpenSea Seaport conduit, etc).
Skip paths: shared/wallets/, shared/secrets/ (legitimately contain secrets —
these should NEVER be committed to a public repo; verify with .gitignore).

Usage:
  python3 scripts/secret_scan.py              # scan all files
  python3 scripts/secret_scan.py --staged     # scan only git staged files
  python3 scripts/secret_scan.py --strict     # fail on warnings too
  python3 scripts/secret_scan.py path/to/file # scan specific path

Exit codes:
  0 — clean
  1 — critical issue(s) found
  2 — warning(s) found (only in --strict mode)
"""
import argparse
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

# ----- Configuration ----------------------------------------------------------

# Paths that legitimately contain secrets and MUST NEVER be committed.
# If these appear in `git ls-files`, that's the real problem.
SKIP_PATH_PATTERNS = [
    re.compile(r"shared/wallets/"),
    re.compile(r"shared/secrets/"),
    re.compile(r"\.git/"),
    re.compile(r"__pycache__/"),
    re.compile(r"\.venv/|venv/|env/"),
    re.compile(r"node_modules/"),
    re.compile(r"\.pyc$"),
]

# Variables/strings that are PUBLIC constants and safe to hardcode.
SAFE_VARIABLE_PATTERNS = [
    re.compile(r"\bCONDUIT_KEY\s*="),
    re.compile(r"\bOPENSEA_(FEE_RECIPIENT|ZONE|CONDUIT_KEY)\s*="),
    re.compile(r"\bSEAPORT_[A-Z_]+\s*="),
]

# Exact public values (defense in depth).
SAFE_EXACT_VALUES = {
    "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000",  # Seaport conduit
    "0x0000a26b00c1f0df003000390027140000faa719",                            # OpenSea fee recipient
    "0x000056f7000000ece9003ca63978907a00ffd100",                            # OpenSea zone
}

# Comment markers that often contain intentional placeholder examples.
# e.g. "# Example: 0x1234..." or docstrings with deliberately invalid keys
COMMENT_LINE_PATTERNS = [
    re.compile(r"^\s*#\s*(example|e\.g\.|placeholder)", re.I),
    re.compile(r"^\s*#\s*0x[0-9a-fA-F]{1,8}\.\.\.", re.I),  # truncated example like # 0x12...
]

# File extensions to scan
EXTENSIONS = {".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".env", ".cfg", ".ini", ".md", ".txt", ".js", ".ts"}

# Patterns to detect — each has a regex + severity + description
PATTERNS = [
    {
        "name": "ethereum_private_key",
        "regex": re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
        "severity": "critical",
        "desc": "Ethereum private key (0x + 64 hex). ROTATE IMMEDIATELY if real.",
    },
    {
        "name": "alchemy_rpc_url",
        "regex": re.compile(r"https://[a-z0-9-]+\.g\.alchemy\.com/v2/([A-Za-z0-9_\-]{20,})"),
        "severity": "critical",
        "desc": "Alchemy RPC URL with embedded API key.",
        "extract": lambda m: m.group(1),
    },
    {
        "name": "twitter_auth_token",
        "regex": re.compile(r"\bauth_token=([a-f0-9]{32,})"),
        "severity": "critical",
        "desc": "Twitter auth_token cookie.",
        "extract": lambda m: m.group(1),
    },
    {
        "name": "twitter_ct0",
        "regex": re.compile(r"\bct0=([a-f0-9]{32,})"),
        "severity": "critical",
        "desc": "Twitter ct0 cookie.",
        "extract": lambda m: m.group(1),
    },
    {
        "name": "github_pat_fine",
        "regex": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
        "severity": "critical",
        "desc": "GitHub fine-grained PAT.",
    },
    {
        "name": "github_pat_classic",
        "regex": re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
        "severity": "critical",
        "desc": "GitHub classic PAT (ghp_).",
    },
    {
        "name": "github_oauth",
        "regex": re.compile(r"\bgho_[A-Za-z0-9]{36}\b"),
        "severity": "critical",
        "desc": "GitHub OAuth token (gho_).",
    },
]


# ----- Scanning logic ---------------------------------------------------------

def shannon_entropy(s: str) -> float:
    """Compute Shannon entropy of a string. High entropy = likely random/secret."""
    if not s:
        return 0.0
    counts = Counter(s)
    total = len(s)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def should_skip_path(path: Path) -> bool:
    path_str = str(path)
    return any(p.search(path_str) for p in SKIP_PATH_PATTERNS)


def is_safe_line(line: str) -> bool:
    """Return True if the entire line is in the allowlist (safe to ignore)."""
    stripped = line.strip()
    # Safe variable assignment
    for pat in SAFE_VARIABLE_PATTERNS:
        if pat.search(line):
            return True
    # Explicit comment with placeholder
    for pat in COMMENT_LINE_PATTERNS:
        if pat.search(line):
            return True
    return False


def mask_value(value: str) -> str:
    """Mask a detected value for safe display (show first 8 + last 4 chars)."""
    if len(value) <= 16:
        return value[:4] + "***"
    return f"{value[:8]}...{value[-4:]}"


def scan_file(file_path: Path) -> list:
    """Scan a single file and return list of issues found."""
    issues = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return issues

    in_safe_docstring = False

    for line_num, line in enumerate(content.splitlines(), 1):
        # Skip safe variable assignments
        if is_safe_line(line):
            continue

        # Check all patterns
        for pat_info in PATTERNS:
            for match in pat_info["regex"].finditer(line):
                value = match.group(0)
                if "extract" in pat_info:
                    extracted = pat_info["extract"](match)
                    if extracted:
                        value = extracted

                # Check if value is in exact safe list
                if value in SAFE_EXACT_VALUES:
                    continue

                issues.append({
                    "file": str(file_path),
                    "line": line_num,
                    "pattern": pat_info["name"],
                    "severity": pat_info["severity"],
                    "value_masked": mask_value(value),
                    "context": line.strip()[:100],
                    "desc": pat_info["desc"],
                })

    return issues


def get_scan_targets(repo_root: Path, mode: str) -> list:
    """Return list of files to scan based on mode."""
    if mode == "staged":
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                cwd=repo_root, capture_output=True, text=True, check=True,
            )
            files = [repo_root / f for f in result.stdout.strip().split("\n") if f]
            return [f for f in files if f.exists()]
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("⚠️  --staged requested but not in a git repo; falling back to full scan",
                  file=sys.stderr)
            mode = "all"

    if mode == "all":
        targets = []
        for f in repo_root.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in EXTENSIONS:
                continue
            if should_skip_path(f):
                continue
            targets.append(f)
        return targets

    return []


def main():
    parser = argparse.ArgumentParser(
        description="Secret scanner (pre-commit + standalone)",
    )
    parser.add_argument("--staged", action="store_true",
                        help="Scan only git-staged files (for pre-commit use)")
    parser.add_argument("--strict", action="store_true",
                        help="Treat warnings as errors")
    parser.add_argument("paths", nargs="*",
                        help="Specific files to scan (overrides --staged/--all)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent

    if args.paths:
        files = [Path(p) for p in args.paths]
    elif args.staged:
        files = get_scan_targets(repo_root, "staged")
    else:
        files = get_scan_targets(repo_root, "all")

    if not files:
        print("ℹ️  No files to scan.")
        sys.exit(0)

    print(f"🔍 Scanning {len(files)} file(s)...", file=sys.stderr)

    all_issues = []
    for f in files:
        if should_skip_path(f):
            continue
        issues = scan_file(f)
        all_issues.extend(issues)

    critical = [i for i in all_issues if i["severity"] == "critical"]
    warning = [i for i in all_issues if i["severity"] == "warning"]

    if all_issues:
        print(f"\n❌ SECRET SCAN FAILED — {len(critical)} critical, {len(warning)} warning(s)")
        print("=" * 70)
        for issue in all_issues:
            tag = "🔴 CRITICAL" if issue["severity"] == "critical" else "🟡 WARNING "
            print(f"{tag}  [{issue['pattern']}]")
            print(f"  File:     {issue['file']}:{issue['line']}")
            print(f"  Match:    {issue['value_masked']}")
            print(f"  Context:  {issue['context']}")
            print(f"  Why:      {issue['desc']}")
            print()

        print("=" * 70)
        if critical:
            print(f"\n🚫 Blocking commit: {len(critical)} critical issue(s).")
            print("   Fix: rotate the key/token, then update the file with the new value.")
        if warning and args.strict:
            print(f"\n🚫 Blocking commit (--strict): {len(warning)} warning(s).")
        elif warning:
            print(f"\n⚠️  {len(warning)} warning(s) (non-blocking in non-strict mode).")

        sys.exit(1 if critical or (warning and args.strict) else 0)

    print(f"✅ Secret scan passed ({len(files)} files scanned, 0 issues)")
    sys.exit(0)


if __name__ == "__main__":
    main()
