#!/usr/bin/env python3
"""Check OpenSea drop stage eligibility via GraphQL, no browser required.

Usage:
  python3 opensea_checker_api.py <wallet_name> <collection_slug>
  python3 opensea_checker_api.py <wallet_name> <slug_1>,<slug_2>,<slug_3>
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from siwe_login import siwe_login

GQL_URL = "https://gql.opensea.io/graphql"
GMT7 = timezone(timedelta(hours=7))

QUERY = """
query DropEligibilityQuery($collectionSlug: String!, $address: Address!) {
  dropBySlug(slug: $collectionSlug) {
    __typename
    ... on Erc721SeaDropV1 {
      minterQuantityMinted(minter: $address)
    }
    stages {
      stageType
      startTime
      maxTotalMintableByWallet
      eligibleMaxTotalMintableByWallet
      isEligible
      label
      stageIndex
      price {
        usd
        token {
          unit
          symbol
        }
      }
      eligiblePrice {
        usd
        token {
          unit
          symbol
        }
      }
    }
  }
}
"""


def split_csv(value: str) -> List[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def fmt_time(value: str) -> str:
    if not value:
        return "TBA"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(GMT7).strftime("%d %b %H:%M GMT+7")


def wei_to_eth(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "TBA"
    # OpenSea GraphQL returns ETH token.unit as a human ETH float, not wei.
    eth = n if n < 1_000_000 else n / 10**18
    if eth == 0:
        return "0.00 ETH"
    return f"{eth:.6f}".rstrip("0").rstrip(".") + " ETH"


def fmt_price(stage: Dict[str, Any]) -> str:
    price = stage.get("eligiblePrice") or stage.get("price") or {}
    token = price.get("token") or {}
    unit = token.get("unit")
    symbol = token.get("symbol") or "ETH"
    if unit is None:
        return "TBA"
    if symbol.upper() == "ETH":
        return wei_to_eth(unit)
    try:
        return f"{int(unit)} {symbol}"
    except (TypeError, ValueError):
        return f"{unit} {symbol}"


def is_eligible(stage: Dict[str, Any]) -> bool:
    if stage.get("isEligible") is not None:
        return bool(stage.get("isEligible"))

    eligible_limit = stage.get("eligibleMaxTotalMintableByWallet")
    if eligible_limit is not None:
        try:
            return int(eligible_limit) > 0
        except (TypeError, ValueError):
            return False

    # Public stages commonly do not need allowlist membership; the global limit applies.
    label = (stage.get("label") or "").lower()
    if "public" in label:
        return int(stage.get("maxTotalMintableByWallet") or 0) > 0
    return False


def stage_limit(stage: Dict[str, Any]) -> Any:
    return stage.get("eligibleMaxTotalMintableByWallet") or stage.get("maxTotalMintableByWallet") or "TBA"


def gql_check(session, address: str, slug: str) -> Dict[str, Any]:
    access_token = session.cookies.get("access_token")
    for domain in (".opensea.io", "opensea.io"):
        session.cookies.set("connected-account-server-hint", address, domain=domain, path="/")
    headers = {
        "content-type": "application/json",
        "x-app-id": "os2-web",
        # These provide the active-wallet GraphQL auth context for eligibility fields.
        "x-active-address": address.lower(),
        "x-wallet-address": address.lower(),
        "x-active-wallet-address": address.lower(),
        "x-user-address": address.lower(),
    }
    if access_token:
        # gql.opensea.io accepts the SIWE JWT here even when cookie auth is not enough.
        headers["x-auth-token"] = access_token
    body = {
        "operationName": "DropEligibilityQuery",
        "query": QUERY,
        "variables": {"collectionSlug": slug, "address": address},
    }
    response = session.post(GQL_URL, json=body, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data


def render_slug(result: Dict[str, Any], slug: str) -> str:
    drop = (result.get("data") or {}).get("dropBySlug")
    if not drop:
        return f"{slug}: not found"

    auth_error = (result.get("extensions") or {}).get("auth", {}).get("error")
    lines = [f"{slug} - https://opensea.io/collection/{slug}/overview"]
    if auth_error:
        lines.append(f"[auth warning] {auth_error.get('classification')}: {auth_error.get('message')}")

    stages = sorted(drop.get("stages") or [], key=lambda s: s.get("startTime") or "")
    for stage in stages:
        icon = "✅" if is_eligible(stage) else "❌"
        label = stage.get("label") or f"Stage {stage.get('stageIndex')}"
        price = fmt_price(stage)
        limit = stage_limit(stage)
        when = fmt_time(stage.get("startTime"))
        lines.append(f"{icon} {label} ({price}, limit {limit}) — {when}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check OpenSea drop eligibility using SIWE + GraphQL.")
    parser.add_argument("wallet", help="Wallet name from nft_config.json")
    parser.add_argument("slugs", help="Comma-separated OpenSea collection slugs")
    args = parser.parse_args()

    session, auth = siwe_login(args.wallet)
    address = auth["address"]
    print(f"Authenticated {args.wallet}: {address}")

    for slug in split_csv(args.slugs):
        try:
            result = gql_check(session, address, slug)
            print("\n" + render_slug(result, slug))
        except Exception as exc:
            print(f"\n{slug}: ERROR {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
