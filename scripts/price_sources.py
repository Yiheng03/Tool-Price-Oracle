#!/usr/bin/env python3
"""Price data layer for Cutting Tool Price Oracle.

Provides a unified cache-backed interface for metal spot prices::

    from price_sources import get_spot_price, cache_price, price_status

    # Check cache
    record = get_spot_price("AL")          # today, if cached
    record = get_spot_price("AL", "2026-06-22")

    # Write cache
    cache_price("AL", 20704, "CNY/ton", "SMM")

    # Status
    status = price_status("AL")            # "available" | "unavailable"

Cache layout::

    .workbuddy/memory/prices/{METAL}_{YYYY-MM-DD}.json

This module does **not** scrape websites — price fetching requires LLM
agents.  When a price is missing from cache the returned PriceRecord has
``status = "unavailable"`` and the caller (or daily_workbuddy_run.py
manifest) instructs the agent to fetch it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PRICES_DIR = ROOT / ".workbuddy" / "memory" / "prices"

METAL_UNITS: dict[str, str] = {
    "CO": "CNY/ton",
    "W": "CNY/kg",
    "NI": "CNY/ton",
    "IRON_ORE": "CNY/ton",
    "CU": "CNY/ton",
    "AL": "CNY/ton",
    "ZN": "CNY/ton",
    "SN": "CNY/ton",
    "PB": "CNY/ton",
}


@dataclass
class PriceRecord:
    metal: str
    price: float
    unit: str
    price_date: str
    source: str
    source_url: str = ""
    quote_type: str = "spot average"
    confidence: str = "medium"
    status: str = "available"       # "available" | "unavailable" | "stale"
    cached_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def unavailable(cls, metal: str, price_date: str, reason: str = "") -> "PriceRecord":
        return cls(
            metal=metal,
            price=0,
            unit=METAL_UNITS.get(metal, ""),
            price_date=price_date,
            source="",
            status="unavailable",
            note=reason,
        )


# -- Cache I/O ----------------------------------------------------------------


def _price_path(metal: str, price_date: str) -> Path:
    return PRICES_DIR / f"{metal}_{price_date}.json"


def get_spot_price(metal: str, price_date: str | None = None) -> PriceRecord:
    """Read a cached price record. Returns PriceRecord(status="unavailable") on miss.

    Args:
        metal: Metal code, e.g. ``"AL"``.
        price_date: Date string ``YYYY-MM-DD``.  Defaults to today.
    """
    if price_date is None:
        price_date = date.today().isoformat()
    path = _price_path(metal.upper(), price_date)
    if not path.exists():
        return PriceRecord.unavailable(metal.upper(), price_date, "not cached")
    try:
        data = _read_json(path)
        return PriceRecord(
            metal=str(data.get("metal", metal)),
            price=float(data.get("price", 0)),
            unit=str(data.get("unit", METAL_UNITS.get(metal, ""))),
            price_date=str(data.get("price_date", price_date)),
            source=str(data.get("source", "")),
            source_url=str(data.get("source_url", "")),
            quote_type=str(data.get("quote_type", "spot average")),
            confidence=str(data.get("confidence", "medium")),
            status=str(data.get("status", "available")),
            cached_at=str(data.get("cached_at", "")),
            note=str(data.get("note", "")),
        )
    except Exception:
        return PriceRecord.unavailable(metal.upper(), price_date, "cache read error")


def cache_price(
    metal: str,
    price: float,
    unit: str = "",
    price_date: str | None = None,
    source: str = "",
    source_url: str = "",
    quote_type: str = "spot average",
    confidence: str = "medium",
    force: bool = False,
) -> Path:
    """Write a price record to the cache.

    Returns the path to the written cache file.
    Does nothing (and returns the existing path) if the cache entry already
    exists and *force* is ``False``.
    """
    if price_date is None:
        price_date = date.today().isoformat()
    metal = metal.upper()
    path = _price_path(metal, price_date)
    if path.exists() and not force:
        return path
    record = PriceRecord(
        metal=metal,
        price=price,
        unit=unit or METAL_UNITS.get(metal, ""),
        price_date=price_date,
        source=source,
        source_url=source_url,
        quote_type=quote_type,
        confidence=confidence,
        status="available",
        cached_at=datetime.now().replace(microsecond=0).isoformat(),
    )
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def price_status(metal: str, price_date: str | None = None) -> str:
    """Return ``"available"``, ``"unavailable"``, or ``"stale"`` for a metal+date."""
    record = get_spot_price(metal, price_date)
    return record.status


def list_cached_metals(price_date: str | None = None) -> list[str]:
    """Return metal codes that have cached prices for *price_date*."""
    if price_date is None:
        price_date = date.today().isoformat()
    if not PRICES_DIR.exists():
        return []
    metals: list[str] = []
    suffix = f"_{price_date}"
    for path in sorted(PRICES_DIR.glob(f"*_{price_date}.json")):
        stem = path.stem  # e.g. "AL_2026-06-23" or "IRON_ORE_2026-06-23"
        metal = stem.removesuffix(suffix)
        metals.append(metal)
    return metals


def build_price_map(price_date: str | None = None) -> dict[str, float]:
    """Return ``{metal: price}`` for all cached prices on *price_date*.

    Useful as input to ``backtest_engine.verify_batch()``.
    """
    if price_date is None:
        price_date = date.today().isoformat()
    price_map: dict[str, float] = {}
    for metal in list_cached_metals(price_date):
        record = get_spot_price(metal, price_date)
        if record.status == "available":
            price_map[metal] = record.price
    return price_map


def build_price_fetch_manifest(
    metals: list[str],
    target_date: str | None = None,
) -> list[dict[str, Any]]:
    """Build agent tasks for fetching missing prices.

    For each metal whose price is not already cached for *target_date*,
    return a task dict instructing the agent to fetch it via spot-price-fetcher
    and then call ``cache_price()`` to persist.

    Returns an empty list when all prices are already cached.
    """
    if target_date is None:
        target_date = date.today().isoformat()
    tasks: list[dict[str, Any]] = []
    for metal in metals:
        record = get_spot_price(metal, target_date)
        if record.status == "available":
            continue
        tasks.append({
            "task_id": f"fetch_price_{metal}_{target_date}",
            "skill": "spot-price-fetcher",
            "action": "fetch_and_cache",
            "metal": metal,
            "target_date": target_date,
            "expected_unit": METAL_UNITS.get(metal, ""),
            "cache_path": str(_price_path(metal, target_date).relative_to(ROOT)),
            "instruction": (
                f"Use spot-price-fetcher to get {metal} spot price for {target_date}. "
                f"Then call cache_price(metal='{metal}', price=<value>, "
                f"unit='{METAL_UNITS.get(metal, '')}', price_date='{target_date}', "
                f"source=<source_name>)."
            ),
        })
    return tasks


# -- Helpers ------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))
