#!/usr/bin/env python3
"""Shared, dependency-free report primitives for NFT Telegram output.

This first phase intentionally has no side effects and no Telegram calls.
"""
from __future__ import annotations

import re
from typing import Any

_STAGE_RE = re.compile(
    r"^[✅❌]\s*(?P<name>.+?)\s*"
    r"\((?P<price>[^,)]*?)\s+(?P<currency>[A-Za-z0-9._-]+),\s*limit\s*(?P<limit>[^)]*)\)\s*—\s*"
    r"(?P<date>\d{1,2}\s+\w+\s+\d{2}:\d{2})\s+(?P<tz>GMT[+-]\d+)$"
)


def parse_stage(raw: Any) -> dict[str, str]:
    """Parse an existing persisted stage string without changing its source data."""
    text = str(raw)
    match = _STAGE_RE.search(text)
    if not match:
        return {
            "name": text,
            "date_time": "",
            "price": "",
            "limit": "",
            "status": "",
        }
    return {
        "name": match.group("name").strip(),
        "date_time": (match.group("date").rsplit(" ", 1)[0] + "\n" + match.group("date").rsplit(" ", 1)[1] + " " + match.group("tz")),
        "price": f"{match.group('price').strip()} {match.group('currency').strip()}",
        "limit": match.group("limit").strip(),
        "status": text[0],
    }


def build_table_rows(entry: dict[str, Any], wallet_display: dict[str, str]) -> list[list[dict[str, Any]]]:
    """Build native Rich Message table cells for one project."""
    wallets = entry.get("wallets", {})
    if not isinstance(wallets, dict):
        wallets = {}
    wallet_keys = list(wallets)
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for wallet in wallet_keys:
        values = wallets[wallet]
        values = values if isinstance(values, list) else [values]
        for raw in values:
            parsed = parse_stage(raw)
            key = parsed["name"].lower()
            if key not in grouped:
                grouped[key] = {"parsed": parsed, "statuses": {}}
                order.append(key)
            grouped[key]["statuses"][wallet] = parsed["status"] or "—"

    def cell(text: Any, align: str = "left", header: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"text": str(text), "align": align}
        if header:
            result["is_header"] = True
        return result

    rows = [[
        cell("Stage", "center", True),
        cell("Date / Time", "center", True),
        cell("Price", "center", True),
        cell("Limit", "center", True),
    ]]
    rows[0].extend(cell(wallet_display.get(wallet, wallet), "center", True) for wallet in wallet_keys)

    for key in order:
        item = grouped[key]
        parsed = item["parsed"]
        row = [
            cell(parsed["name"], "left"),
            cell(parsed["date_time"], "center"),
            cell(parsed["price"], "left"),
            cell(parsed["limit"], "center"),
        ]
        row.extend(cell(item["statuses"].get(wallet, "—"), "center") for wallet in wallet_keys)
        rows.append(row)
    return rows


def rich_text_from_markdown(text: str) -> list[Any]:
    """Convert the project header's Markdown links to native RichText URLs."""
    result: list[Any] = []
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            result.append(text[cursor:match.start()])
        result.append({"type": "url", "text": match.group(1), "url": match.group(2)})
        cursor = match.end()
    if cursor < len(text):
        result.append(text[cursor:])
    return result or [text]


def build_table_block(entry: dict[str, Any], wallet_display: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "table",
        "is_bordered": True,
        "is_striped": True,
        "cells": build_table_rows(entry, wallet_display),
    }


def filtered_entry(entry: dict[str, Any], wallets: list[str] | None = None, today_only: bool = False, stage_today=None) -> dict[str, Any] | None:
    """Return a report-only copy filtered by wallet/date; never mutates persisted data."""
    source = entry.get("wallets", {}) if isinstance(entry.get("wallets"), dict) else {}
    selected = wallets or list(source)
    result_wallets: dict[str, list[Any]] = {}
    for wallet in selected:
        values = source.get(wallet, [])
        values = values if isinstance(values, list) else [values]
        if today_only and stage_today is not None:
            values = [value for value in values if stage_today(value)]
        if values:
            result_wallets[wallet] = values
    if not result_wallets:
        return None
    return {**entry, "wallets": result_wallets}


__all__ = ["parse_stage", "build_table_rows", "build_table_block", "filtered_entry", "rich_text_from_markdown"]

if __name__ == "__main__":
    sample = {
        "wallets": {
            "wallet_a": ["✅ WL (0.00 ETH, limit 1) — 22 Aug 20:00 GMT+7"],
            "wallet_b": ["❌ WL (0.00 ETH, limit 1) — 22 Aug 20:00 GMT+7"],
        }
    }
    import json
    print(json.dumps(build_table_block(sample, {"wallet_a": "Wallet A", "wallet_b": "Wallet B"}), ensure_ascii=False, indent=2))


def _test_import() -> None:
    assert parse_stage("✅ WL (0.00 ETH, limit 1) — 22 Aug 20:00 GMT+7")["date_time"] == "22 Aug\n20:00 GMT+7"
    hype = parse_stage("✅ Public Stage (0 HYPE, limit 7) — 08 Sep 01:00 GMT+7")
    assert hype == {"name": "Public Stage", "date_time": "08 Sep\n01:00 GMT+7", "price": "0 HYPE", "limit": "7", "status": "✅"}
    assert build_table_rows({"wallets": {}}, {})[0][0]["text"] == "Stage"


_test_import()
