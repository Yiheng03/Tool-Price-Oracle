#!/usr/bin/env python3
"""Backtest engine for one-day metal price prediction verification.

Core responsibilities:
  1. Read a D-day prediction from ``predictions/{METAL}_{D}_1d.json``.
  2. Given today's actual spot price, compute the verification result.
  3. Write the result to ``results/{METAL}_{D}_1d.json``.

Can be used as a library (import ``verify_prediction``, ``verify_from_file``)
or as a CLI for single-metal and batch verification.

CLI examples::

  py backtest_engine.py --date 2026-06-23 --metal AL --actual-price 20704
  py backtest_engine.py --date 2026-06-23 --batch price_map.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# -- Paths ---------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
BACKTEST_DIR = ROOT / ".workbuddy" / "memory" / "backtest"
PREDICTIONS_DIR = BACKTEST_DIR / "predictions"
RESULTS_DIR = BACKTEST_DIR / "results"

# -- Constants -----------------------------------------------------------------
FLAT_THRESHOLD = 0.005
PRED_SUFFIX = "_1d.json"


# -- Core computation ----------------------------------------------------------


def compute_direction(change_pct: float, flat_threshold: float = FLAT_THRESHOLD) -> str:
    """Map a price change percentage to a direction string.

    >>> compute_direction(0.02)
    'up'
    >>> compute_direction(-0.01)
    'down'
    >>> compute_direction(0.002)
    'flat'
    """
    if abs(change_pct) <= flat_threshold:
        return "flat"
    return "up" if change_pct > 0 else "down"


def verify_prediction(
    prediction: dict[str, Any],
    actual_price: float,
    actual_source: str = "",
    *,
    flat_threshold: float = FLAT_THRESHOLD,
) -> dict[str, Any]:
    """Compute a verification result from a prediction dict and actual price.

    This is the single source of truth for hit/miss computation.  Every path
    (library call, CLI single, CLI batch) funnels through this function.

    Args:
        prediction: Parsed prediction JSON with keys ``metal``, ``date_d``,
            ``target_date``, ``price_d``, ``predicted_direction``,
            ``predicted_range_pct``.
        actual_price: Today's (D+1) actual spot price.
        actual_source: Data source label for the actual price, e.g. ``\"SMM\"``.
        flat_threshold: Maximum absolute change to qualify as ``\"flat\"``.

    Returns:
        A result dict ready to write to
        ``results/{METAL}_{prediction_date}_1d.json``.
    """
    metal = str(prediction.get("metal", ""))
    date_d = str(prediction.get("date_d", ""))
    target_date = str(prediction.get("target_date", ""))
    price_d = float(prediction.get("price_d", 0))
    predicted_direction = str(prediction.get("predicted_direction", ""))
    predicted_range = _parse_range(prediction.get("predicted_range_pct"))

    # 1. Actual change
    if price_d == 0:
        actual_change_pct = 0.0
    else:
        actual_change_pct = (actual_price - price_d) / price_d

    # 2. Direction
    actual_direction = compute_direction(actual_change_pct, flat_threshold)
    hit_direction = (predicted_direction == actual_direction)

    # 3. Range
    low, high = predicted_range
    hit_range = low <= actual_change_pct <= high

    # 4. Error (signed — negative = below predicted range, positive = above)
    if hit_range:
        error_pct = 0.0
    elif actual_change_pct < low:
        error_pct = actual_change_pct - low   # negative
    else:
        error_pct = actual_change_pct - high  # positive

    return {
        "metal": metal,
        "prediction_date": date_d,
        "actual_date": target_date,
        "price_d": price_d,
        "price_d_plus_1": actual_price,
        "actual_change_pct": round(actual_change_pct, 6),
        "predicted_direction": predicted_direction,
        "predicted_range_pct": predicted_range,
        "actual_direction": actual_direction,
        "hit_direction": hit_direction,
        "hit_range": hit_range,
        "error_pct": round(error_pct, 6),
        "actual_price_source": actual_source,
        "bias_reason": "",
        "next_adjustment": "",
        "status": "verified",
    }


def verify_from_file(
    prediction_path: Path,
    actual_price: float,
    actual_source: str = "",
    *,
    flat_threshold: float = FLAT_THRESHOLD,
) -> dict[str, Any]:
    """Read a prediction JSON, verify it, and return the result dict.

    Does **not** write to disk — call ``write_result()`` to persist.
    """
    prediction = _read_json(prediction_path)
    return verify_prediction(
        prediction,
        actual_price,
        actual_source,
        flat_threshold=flat_threshold,
    )


# -- Batch verification --------------------------------------------------------


def verify_batch(
    today_date: str,
    price_map: dict[str, float],
    *,
    source_map: dict[str, str] | None = None,
    flat_threshold: float = FLAT_THRESHOLD,
) -> list[dict[str, Any]]:
    """Verify all predictions whose ``target_date`` matches *today_date*.

    Args:
        today_date: D+1 date to check, e.g. ``\"2026-06-23\"``.
        price_map: ``{metal_code: actual_price}``, e.g. ``{\"AL\": 20704}``.
        source_map: Optional ``{metal_code: source_name}``.
        flat_threshold: Flat-direction threshold.

    Returns:
        List of result dicts, one per verified prediction.  Metals in
        *price_map* that have no matching prediction are silently skipped.
    """
    results: list[dict[str, Any]] = []
    if source_map is None:
        source_map = {}
    if not PREDICTIONS_DIR.exists():
        return results
    for path in sorted(PREDICTIONS_DIR.glob(f"*{PRED_SUFFIX}")):
        try:
            pred = _read_json(path)
        except Exception:
            print(f"Warning: skipping unreadable prediction: {path}", file=sys.stderr)
            continue
        if str(pred.get("target_date", "")) != today_date:
            continue
        metal = str(pred.get("metal", ""))
        if metal not in price_map:
            continue
        result = verify_prediction(
            pred,
            price_map[metal],
            source_map.get(metal, ""),
            flat_threshold=flat_threshold,
        )
        results.append(result)
    return results


def verify_batch_and_write(
    today_date: str,
    price_map: dict[str, float],
    *,
    source_map: dict[str, str] | None = None,
    force: bool = False,
) -> list[Path]:
    """Verify and persist all results for *today_date* in one call."""
    results = verify_batch(today_date, price_map, source_map=source_map)
    written: list[Path] = []
    for result in results:
        path = write_result(result, force=force)
        if path:
            written.append(path)
    return written


# -- File I/O ------------------------------------------------------------------


def prediction_path(metal: str, date_d: str) -> Path:
    return PREDICTIONS_DIR / f"{metal}_{date_d}{PRED_SUFFIX}"


def result_path(metal: str, prediction_date: str) -> Path:
    return RESULTS_DIR / f"{metal}_{prediction_date}{PRED_SUFFIX}"


def result_exists(metal: str, prediction_date: str) -> bool:
    return result_path(metal, prediction_date).exists()


def write_result(result: dict[str, Any], force: bool = False) -> Path | None:
    """Persist a verification result dict to ``results/{METAL}_{D}_1d.json``.

    Returns ``None`` if the file already exists and *force* is ``False``.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    metal = str(result.get("metal", ""))
    pred_date = str(result.get("prediction_date", ""))
    path = result_path(metal, pred_date)
    if path.exists() and not force:
        return None
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_result(metal: str, prediction_date: str) -> dict[str, Any] | None:
    """Read an existing result file, or ``None`` if it does not exist."""
    path = result_path(metal, prediction_date)
    if not path.exists():
        return None
    try:
        return _read_json(path)
    except Exception:
        return None


def find_missed_metals(today_date: str) -> list[dict[str, Any]]:
    """Return all result dicts for *today_date* that missed direction or range."""
    missed: list[dict[str, Any]] = []
    if not RESULTS_DIR.exists():
        return missed
    for path in sorted(RESULTS_DIR.glob(f"*{PRED_SUFFIX}")):
        try:
            r = _read_json(path)
        except Exception:
            continue
        if str(r.get("actual_date", "")) != today_date:
            continue
        if not r.get("hit_direction") or not r.get("hit_range"):
            missed.append(r)
    return missed


# -- Helpers -------------------------------------------------------------------


def _parse_range(value: Any) -> list[float]:
    if isinstance(value, list) and len(value) >= 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            pass
    return [0.0, 0.0]


def _read_json(path: Path) -> dict[str, Any]:
    """Read JSON, tolerating UTF-8 BOM."""
    raw = path.read_bytes()
    # Strip BOM if present
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def _add_days(date_str: str, days: int) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (d + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return date_str


# -- CLI -----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest engine — verify D+1 metal price predictions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date",
        type=str,
        default=date.today().isoformat(),
        help="Target (D+1) date YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--metal",
        type=str,
        default=None,
        help="Metal code for single-metal verification, e.g. AL.",
    )
    parser.add_argument(
        "--actual-price",
        type=float,
        default=None,
        help="Actual spot price for --metal on --date.",
    )
    parser.add_argument(
        "--actual-source",
        type=str,
        default="",
        help="Source label for the actual price, e.g. SMM.",
    )
    parser.add_argument(
        "--batch",
        type=str,
        default=None,
        metavar="PRICE_MAP_JSON",
        help="Path to a JSON file mapping metal codes to actual prices. "
        "Format: {\"AL\": 20704, \"CU\": 80100, ...}",
    )
    parser.add_argument(
        "--source-map",
        type=str,
        default=None,
        metavar="SOURCE_MAP_JSON",
        help="Optional JSON file mapping metal codes to price source labels.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing result files.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Output only the result JSON (suppress summary line).",
    )
    parser.add_argument(
        "--flat-threshold",
        type=float,
        default=FLAT_THRESHOLD,
        help=f"Max absolute pct for 'flat' direction (default: {FLAT_THRESHOLD}).",
    )
    args = parser.parse_args()

    # Single-metal mode
    if args.metal and args.actual_price is not None:
        pred_path = prediction_path(args.metal.upper(), _add_days(args.date, -1))
        if not pred_path.exists():
            print(
                f"No prediction found: {pred_path}",
                file=sys.stderr,
            )
            # Also try scanning all predictions for this target date
            found = False
            for p in sorted(PREDICTIONS_DIR.glob(f"*{PRED_SUFFIX}")):
                try:
                    pred = _read_json(p)
                except Exception:
                    continue
                if (
                    str(pred.get("metal", "")) == args.metal.upper()
                    and str(pred.get("target_date", "")) == args.date
                ):
                    pred_path = p
                    found = True
                    break
            if not found:
                return 1
        result = verify_from_file(
            pred_path,
            args.actual_price,
            args.actual_source,
            flat_threshold=args.flat_threshold,
        )
        path = write_result(result, force=args.force)
        if not args.json_only:
            hit_parts = []
            if result["hit_direction"]:
                hit_parts.append("direction HIT")
            else:
                hit_parts.append("direction MISS")
            if result["hit_range"]:
                hit_parts.append("range HIT")
            else:
                hit_parts.append("range MISS")
            print(
                f"{args.date} | {result['metal']}: "
                f"predicted {result['predicted_direction']}, "
                f"actual {result['actual_direction']} "
                f"({result['actual_change_pct']:+.4%}) | "
                f"{', '.join(hit_parts)} | "
                f"error={result['error_pct']:+.4%}"
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if path:
            print(f"", file=sys.stderr)  # suppress if no write
        return 0

    # Batch mode
    if args.batch:
        price_map = _read_json(Path(args.batch))
        source_map = {}
        if args.source_map:
            source_map = _read_json(Path(args.source_map))
        results = verify_batch(
            args.date,
            price_map,
            source_map=source_map,
            flat_threshold=args.flat_threshold,
        )
        if not results:
            print(
                f"{args.date}: No predictions matched the price map.",
            )
            return 0
        written = []
        for result in results:
            path = write_result(result, force=args.force)
            if path:
                written.append(path)
        dir_hits = sum(1 for r in results if r["hit_direction"])
        rng_hits = sum(1 for r in results if r["hit_range"])
        missed = [
            f"{r['metal']}-{_miss_short(r)}"
            for r in results
            if not r["hit_direction"] or not r["hit_range"]
        ]
        if not args.json_only:
            summary = (
                f"{args.date} Review: {len(results)} verified — "
                f"{dir_hits} direction hits, {rng_hits} range hits"
            )
            if missed:
                summary += f". Misses: [{', '.join(missed)}]"
            print(summary)
            print(f"Wrote {len(written)} result files.")
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    # No action specified
    parser.print_help()
    return 1


def _miss_short(result: dict[str, Any]) -> str:
    hit_dir = result.get("hit_direction", False)
    hit_rng = result.get("hit_range", False)
    if not hit_dir and not hit_rng:
        return "both"
    if not hit_dir:
        return "direction"
    return "range"


if __name__ == "__main__":
    raise SystemExit(main())
