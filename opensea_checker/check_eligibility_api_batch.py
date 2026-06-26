#!/usr/bin/env python3
"""OpenSea eligibility batch checker via SIWE + GraphQL, no browser.

Output intentionally matches check_eligibility_batch.py so nft_cron can keep the
same parser and Telegram format.
"""
import sys
from pathlib import Path
from typing import Dict, List

from check_eligibility_api import fmt_price, fmt_time, gql_check, is_eligible, stage_limit
from siwe_login import siwe_login

BASE_DIR = Path(__file__).resolve().parents[1]
LOG_PATH = BASE_DIR / "opensea_api_errors.log"


def wallet_display_name(wallet_key: str) -> str:
    return wallet_key.replace("_", " ").replace("-", " ").title()


def log_error(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(message.rstrip() + "\n")


def project_name_from_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def check_wallet(wallet_key: str, slugs: List[str]) -> Dict[str, dict]:
    results: Dict[str, dict] = {}
    try:
        session, auth = siwe_login(wallet_key)
    except Exception as exc:
        msg = f"[{wallet_key}] SIWE login failed: {type(exc).__name__}: {exc}"
        print(f"[!] {msg}", file=sys.stderr)
        log_error(msg)
        for slug in slugs:
            results[slug] = {"error": "OpenSea API login failed"}
        return results

    for slug in slugs:
        try:
            data = gql_check(session, auth["address"], slug)
            drop = (data.get("data") or {}).get("dropBySlug")
            if not drop:
                results[slug] = {"error": "dropBySlug returned no data"}
                continue
            stages = drop.get("stages") or []
            results[slug] = {
                "project": project_name_from_slug(slug),
                "chain": "Ethereum",
                "stages": stages,
            }
            auth_warning = (data.get("extensions") or {}).get("auth")
            if auth_warning:
                log_error(f"[{wallet_key}/{slug}] GraphQL auth warning: {auth_warning}")
        except Exception as exc:
            msg = f"[{wallet_key}/{slug}] OpenSea API check failed: {type(exc).__name__}: {exc}"
            print(f"[!] {msg}", file=sys.stderr)
            log_error(msg)
            results[slug] = {"error": "OpenSea API check failed"}
    return results


def render_report(all_results: Dict[str, Dict[str, dict]], slugs: List[str], wallet_keys: List[str]) -> str:
    lines = []
    for idx, slug in enumerate(slugs, start=1):
        project_data = None
        for wallet_key in wallet_keys:
            candidate = all_results.get(wallet_key, {}).get(slug, {})
            if candidate.get("project"):
                project_data = candidate
                break
        if not project_data:
            project_data = {"project": project_name_from_slug(slug), "chain": "Ethereum"}

        link = f"https://opensea.io/collection/{slug}/overview"
        lines.append(f"{idx}. [{project_data['project']}]({link}) — {project_data.get('chain', 'Ethereum')}")

        for wallet_key in wallet_keys:
            label = wallet_display_name(wallet_key)
            data = all_results.get(wallet_key, {}).get(slug, {})
            lines.append(f"**{label}:**")
            stages = data.get("stages") or []
            if not stages:
                lines.append(f"  (no data: {data.get('error', 'no stages parsed')})")
                continue
            for stage in stages:
                icon = "✅" if is_eligible(stage) else "❌"
                label_text = stage.get("label") or f"Stage {stage.get('stageIndex')}"
                lines.append(
                    f"{icon} {label_text} ({fmt_price(stage)}, limit {stage_limit(stage)}) — {fmt_time(stage.get('startTime'))}"
                )
        lines.append("")
    return "\n".join(lines)


def main(wallet_csv: str, slug_csv: str) -> str:
    wallet_keys = [w.strip().lower() for w in wallet_csv.split(",") if w.strip()]
    slugs = [s.strip() for s in slug_csv.split(",") if s.strip()]
    all_results = {}
    for wallet_key in wallet_keys:
        all_results[wallet_key] = check_wallet(wallet_key, slugs)

    report = render_report(all_results, slugs, wallet_keys)
    print("\n" + "=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print(report)
    Path("/tmp/elig_report.txt").write_text(report)
    return report


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 check_eligibility_api_batch.py <wallet_csv> <slug_csv>")
        sys.exit(1)
    wallet_csv = sys.argv[1]
    slug_csv = sys.argv[2] if len(sys.argv) == 3 else ",".join(sys.argv[2:])
    main(wallet_csv, slug_csv)
