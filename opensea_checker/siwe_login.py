#!/usr/bin/env python3
"""OpenSea SIWE login helper.

Creates an authenticated requests.Session and returns the OpenSea access_token JWT
cookie without launching a browser.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

CONFIG_PATH = Path(os.environ.get("NFT_CONFIG_PATH", Path(__file__).resolve().parents[2] / "nft_config.json"))
OPENSEA_ORIGIN = "https://opensea.io"
NONCE_URL = f"{OPENSEA_ORIGIN}/__api/auth/siwe/nonce"
VERIFY_URL = f"{OPENSEA_ORIGIN}/__api/auth/siwe/verify"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
STATEMENT = (
    "Click to sign in and accept the OpenSea Terms of Service "
    "(https://opensea.io/tos) and Privacy Policy (https://opensea.io/privacy)."
)


def load_wallet(wallet_name: str, config_path: Path = CONFIG_PATH) -> Dict[str, str]:
    with config_path.open() as f:
        config = json.load(f)

    wallets = config.get("wallets", {})
    lookup = {name.lower(): data for name, data in wallets.items()}
    wallet = lookup.get(wallet_name.lower())
    if not wallet:
        raise SystemExit(f"Wallet '{wallet_name}' not found in {config_path}")
    if not wallet.get("address") or not wallet.get("private_key"):
        raise SystemExit(f"Wallet '{wallet_name}' is missing address/private_key")
    return wallet


def _issued_at() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def build_siwe_message(address: str, nonce: str, chain_id: int = 1) -> Tuple[str, Dict[str, str]]:
    issued_at = _issued_at()
    message_text = (
        "opensea.io wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        f"{STATEMENT}\n\n"
        "URI: https://opensea.io/\n"
        "Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )
    message_obj = {
        "domain": "opensea.io",
        "address": address,
        "statement": STATEMENT,
        "uri": "https://opensea.io/",
        "version": "1",
        "chainId": str(chain_id),
        "nonce": nonce,
        "issuedAt": issued_at,
        "accountType": "Ethereum",
    }
    return message_text, message_obj


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "user-agent": USER_AGENT,
            "accept": "application/json",
            "origin": OPENSEA_ORIGIN,
            "referer": f"{OPENSEA_ORIGIN}/",
        }
    )
    return session


def siwe_login(
    wallet_name: str,
    *,
    config_path: Path = CONFIG_PATH,
    session: Optional[requests.Session] = None,
) -> Tuple[requests.Session, Dict[str, str]]:
    wallet = load_wallet(wallet_name, config_path)
    address = wallet["address"]
    private_key = wallet["private_key"]
    session = session or create_session()

    nonce_response = session.post(NONCE_URL, timeout=30)
    nonce_response.raise_for_status()
    nonce = nonce_response.json()["nonce"]

    message_text, message_obj = build_siwe_message(address, nonce)
    signed = Account.sign_message(encode_defunct(text=message_text), private_key=private_key)
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    verify_body = {
        "message": message_obj,
        "signature": signature,
        "chainArch": "EVM",
        "connectorId": "metamask",
    }
    verify_response = session.post(
        VERIFY_URL,
        json=verify_body,
        headers={"content-type": "application/json"},
        timeout=30,
    )
    verify_response.raise_for_status()

    access_token = session.cookies.get("access_token")
    if not access_token:
        raise RuntimeError(f"SIWE succeeded but access_token cookie missing: {verify_response.text[:300]}")

    return session, {"address": address, "access_token": access_token, "user": verify_response.json().get("user", {})}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Login to OpenSea with SIWE and print JWT cookie.")
    parser.add_argument("wallet", help="Wallet name from nft_config.json")
    args = parser.parse_args()

    _, result = siwe_login(args.wallet)
    print(f"wallet={args.wallet}")
    print(f"address={result['address']}")
    print(f"access_token={result['access_token']}")


if __name__ == "__main__":
    main()
