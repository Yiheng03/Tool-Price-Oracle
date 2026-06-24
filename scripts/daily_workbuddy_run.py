#!/usr/bin/env python3
"""Daily WorkBuddy orchestrator for Cutting Tool Price Oracle.

Phase-based automation of the full closed-loop workflow:
  --phase verify   Steps 1-5: find yesterday's predictions, compute hit/miss,
                   write results and learnings.
  --phase predict  Steps 6-9: read learnings, prepare prediction context,
                   write new predictions for tomorrow.
  --phase report   Step 10:  assemble daily_briefing report JSON and call
                   build_html_report.py for HTML generation.
  (default)        Steps 1-9 combined: verify + predict manifests.

First stdout line is always yesterday's review summary.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Allow importing build_html_report from the same scripts/ directory
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import backtest_engine
import prediction_engine
import price_sources

# -- Paths (mirror build_html_report.py layout) ---------------------------------
ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "daily_run.json"
BACKTEST_DIR = ROOT / ".workbuddy" / "memory" / "backtest"
PREDICTIONS_DIR = BACKTEST_DIR / "predictions"
RESULTS_DIR = BACKTEST_DIR / "results"
LEARNINGS_DIR = BACKTEST_DIR / "learnings"
REPORT_DIR = ROOT / ".workbuddy" / "memory" / "reports"
SIGNALS_DIR = ROOT / ".workbuddy" / "memory" / "signals"
MODEL_OUTPUT_DIR = ROOT / ".workbuddy" / "memory" / "model_outputs"
BIAS_REVIEW_DIR = MODEL_OUTPUT_DIR / "bias_review"
PREDICTION_OUTPUT_DIR = MODEL_OUTPUT_DIR / "predictions"

# -- Config ------------------------------------------------------------------
_DEFAULT_METALS = ["CO", "W", "NI", "IRON_ORE", "CU", "AL", "ZN", "SN", "PB"]


def load_config() -> dict[str, Any]:
    """Load daily_run.json, returning defaults for any missing keys."""
    if not CONFIG_PATH.exists():
        return _default_config()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_config()
    cfg = _default_config()
    if isinstance(raw.get("metals"), list):
        cfg["metals"] = [str(m).strip().upper() for m in raw["metals"] if str(m).strip()]
    if isinstance(raw.get("default_flat_threshold"), (int, float)):
        cfg["default_flat_threshold"] = float(raw["default_flat_threshold"])
    if isinstance(raw.get("price_source_priority"), list):
        cfg["price_source_priority"] = [str(s) for s in raw["price_source_priority"]]
    if isinstance(raw.get("report_type"), str) and raw["report_type"].strip():
        cfg["report_type"] = raw["report_type"].strip()
    if isinstance(raw.get("require_yesterday_review_first"), bool):
        cfg["require_yesterday_review_first"] = raw["require_yesterday_review_first"]
    # Nested sections
    for section in ("backtest", "report"):
        if isinstance(raw.get(section), dict):
            cfg[section].update(
                {k: v for k, v in raw[section].items() if k in cfg[section]}
            )
    return cfg


def _default_config() -> dict[str, Any]:
    return {
        "metals": list(_DEFAULT_METALS),
        "default_flat_threshold": 0.005,
        "price_source_priority": ["100ppi", "smm", "manual_cache"],
        "report_type": "daily_briefing",
        "require_yesterday_review_first": True,
        "backtest": {
            "enabled": True,
            "auto_write_results": True,
            "alert_thresholds": {
                "direction_miss_streak_warn": 3,
                "direction_miss_streak_critical": 5,
                "mape_warn_pct": 10.0,
                "mape_critical_pct": 15.0,
            },
        },
        "report": {
            "output_dir": ".workbuddy/memory/reports",
            "build_html": True,
            "refresh_index": True,
            "auto_open_html": True,
            "publish_workbuddy_sidecar": True,
            "workbuddy_root": str(Path.home() / "WorkBuddy"),
        },
    }


_CONFIG: dict[str, Any] | None = None


def get_config() -> dict[str, Any]:
    """Return the cached config, loading from disk on first call."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


# -- Constants ------------------------------------------------------------------
METALS: list[str] = []  # populated below from config
FLAT_THRESHOLD = backtest_engine.FLAT_THRESHOLD
PRED_SUFFIX = backtest_engine.PRED_SUFFIX
LEARNINGS_SUFFIX = "_learnings.md"

# Re-export path constants from backtest_engine for internal use
PREDICTIONS_DIR = backtest_engine.PREDICTIONS_DIR
RESULTS_DIR = backtest_engine.RESULTS_DIR


def _init_from_config() -> None:
    """Populate module-level METALS and FLAT_THRESHOLD from daily_run.json."""
    cfg = get_config()
    METALS[:] = cfg["metals"]
    global FLAT_THRESHOLD
    FLAT_THRESHOLD = cfg["default_flat_threshold"]


_init_from_config()

# -- Dataclasses ----------------------------------------------------------------


@dataclass
class PredictionRecord:
    metal: str
    date_d: str
    target_date: str
    price_d: float
    price_unit: str = ""
    price_source: str = ""
    predicted_direction: str = ""
    predicted_range_pct: list[float] = field(default_factory=lambda: [0.0, 0.0])
    confidence: str = ""
    signals: list[dict[str, str]] = field(default_factory=list)
    used_learnings: list[str] = field(default_factory=list)
    rationale: str = ""
    status: str = "pending"
    source_file: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], source_file: str = "") -> "PredictionRecord":
        signals = _parse_signal_list(data.get("signals"))
        if not signals:
            signal_briefs = _parse_str_list(data.get("news_summary")) + _parse_str_list(
                data.get("supply_chain_summary")
            )
            signals = [{"brief": s} for s in signal_briefs]
        return cls(
            metal=str(data.get("metal", "")),
            date_d=str(data.get("date_d", "")),
            target_date=str(data.get("target_date", "")),
            price_d=float(data.get("price_d", 0)),
            price_unit=str(data.get("price_unit", "")),
            price_source=str(data.get("price_source", "")),
            predicted_direction=str(data.get("predicted_direction", "")),
            predicted_range_pct=_parse_range(data.get("predicted_range_pct")),
            confidence=str(data.get("confidence", "")),
            signals=signals,
            used_learnings=_parse_str_list(data.get("used_learnings")),
            rationale=str(data.get("rationale", "")),
            status=str(data.get("status", "pending")),
            source_file=source_file,
        )


@dataclass
class ResultRecord:
    metal: str
    prediction_date: str
    actual_date: str
    price_d: float
    price_d_plus_1: float
    actual_change_pct: float
    predicted_direction: str
    predicted_range_pct: list[float] = field(default_factory=lambda: [0.0, 0.0])
    actual_direction: str = ""
    hit_direction: bool = False
    hit_range: bool = False
    error_pct: float = 0.0
    actual_price_source: str = ""
    bias_reason: str = ""
    next_adjustment: str = ""
    status: str = "verified"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultRecord":
        return cls(
            metal=str(data.get("metal", "")),
            prediction_date=str(data.get("prediction_date", "")),
            actual_date=str(data.get("actual_date", "")),
            price_d=float(data.get("price_d", 0)),
            price_d_plus_1=float(data.get("price_d_plus_1", 0)),
            actual_change_pct=float(data.get("actual_change_pct", 0)),
            predicted_direction=str(data.get("predicted_direction", "")),
            predicted_range_pct=_parse_range(data.get("predicted_range_pct")),
            actual_direction=str(data.get("actual_direction", "")),
            hit_direction=bool(data.get("hit_direction", False)),
            hit_range=bool(data.get("hit_range", False)),
            error_pct=float(data.get("error_pct", 0)),
            actual_price_source=str(data.get("actual_price_source", "")),
            bias_reason=str(data.get("bias_reason", "")),
            next_adjustment=str(data.get("next_adjustment", "")),
            status=str(data.get("status", "verified")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LearningEntry:
    date: str
    metal: str
    miss_type: str
    prediction_text: str = ""
    actual_text: str = ""
    main_reason: str = ""
    next_adjustment: str = ""


@dataclass
class YesterdayReview:
    date: str
    total_predictions: int = 0
    verified_count: int = 0
    pending_count: int = 0
    hit_direction_count: int = 0
    hit_range_count: int = 0
    missed_metals: list[str] = field(default_factory=list)
    summary_text: str = ""
    headline: str = ""


# -- Helpers -------------------------------------------------------------------


def _parse_range(value: Any) -> list[float]:
    if isinstance(value, list) and len(value) >= 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            pass
    return [0.0, 0.0]


def _parse_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _parse_signal_list(value: Any) -> list[dict[str, str]]:
    """Parse signals from standard objects or compact string lists.

    Standard format: [{"source": "news", "event": "...", "direction": "up", "strength": "high", "timing": "D-day"}, ...]
    Compact format: ["string signal 1", "string signal 2"]
    """
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            result.append({k: str(v) for k, v in item.items()})
        elif isinstance(item, str):
            result.append({"brief": item})
    return result


def _format_signals_text(signals: list[dict[str, str]]) -> str:
    """Format structured signals as human-readable text for display and prompts."""
    if not signals:
        return "  (none recorded)"
    lines: list[str] = []
    for sig in signals:
        source = sig.get("source", "")
        direction = sig.get("direction", "")
        strength = sig.get("strength", "")
        brief = sig.get("brief", "") or sig.get("event", "")
        dir_label = {"up": "↑", "down": "↓", "neutral": "→"}.get(direction, "")
        lines.append(f"  [{source}] {dir_label} {brief} (strength={strength})")
    return "\n".join(lines)


# -- Part A: File I/O Layer ----------------------------------------------------


def ensure_directories() -> None:
    for d in (
        PREDICTIONS_DIR,
        RESULTS_DIR,
        LEARNINGS_DIR,
        REPORT_DIR,
        SIGNALS_DIR,
        BIAS_REVIEW_DIR,
        PREDICTION_OUTPUT_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def find_yesterdays_predictions(today_date: str) -> list[tuple[Path, PredictionRecord]]:
    """Scan predictions/ for *_1d.json files whose target_date matches today_date."""
    results: list[tuple[Path, PredictionRecord]] = []
    if not PREDICTIONS_DIR.exists():
        return results
    for path in sorted(PREDICTIONS_DIR.glob(f"*{PRED_SUFFIX}")):
        try:
            pred = load_prediction(path)
        except Exception:
            print(f"Warning: skipping corrupt prediction file: {path}", file=sys.stderr)
            continue
        if pred.target_date == today_date:
            results.append((path, pred))
    return results


def find_todays_predictions(today_date: str) -> list[tuple[Path, PredictionRecord]]:
    """Scan predictions/ for *_1d.json files whose date_d matches today_date."""
    results: list[tuple[Path, PredictionRecord]] = []
    if not PREDICTIONS_DIR.exists():
        return results
    for path in sorted(PREDICTIONS_DIR.glob(f"*{PRED_SUFFIX}")):
        try:
            pred = load_prediction(path)
        except Exception:
            continue
        if pred.date_d == today_date:
            results.append((path, pred))
    return results


def load_prediction(path: Path) -> PredictionRecord:
    data = _read_json(path)
    return PredictionRecord.from_dict(data, source_file=str(path.relative_to(ROOT)))


def load_result(path: Path) -> ResultRecord:
    data = backtest_engine._read_json(path)
    return ResultRecord.from_dict(data)


def _read_json(path: Path) -> dict[str, Any]:
    return backtest_engine._read_json(path)


def result_path(metal: str, prediction_date: str) -> Path:
    return backtest_engine.result_path(metal, prediction_date)


def result_exists(metal: str, prediction_date: str) -> bool:
    return backtest_engine.result_exists(metal, prediction_date)


def load_existing_result(metal: str, prediction_date: str) -> ResultRecord | None:
    d = backtest_engine.load_result(metal, prediction_date)
    if d is None:
        return None
    return ResultRecord.from_dict(d)


def prediction_path(metal: str, date_d: str) -> Path:
    return backtest_engine.prediction_path(metal, date_d)


def prediction_exists(metal: str, date_d: str) -> bool:
    return prediction_path(metal, date_d).exists()


def write_result(result: ResultRecord, force: bool = False) -> Path:
    backtest_engine.write_result(result.to_dict(), force=force)
    return result_path(result.metal, result.prediction_date)


def write_prediction(pred: PredictionRecord) -> Path:
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    p = prediction_path(pred.metal, pred.date_d)
    p.write_text(json.dumps(_prediction_to_dict(pred), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def record_prediction(
    metal: str,
    price_d: float,
    predicted_direction: str,
    predicted_range_pct: list[float],
    confidence: str,
    rationale: str,
    *,
    price_unit: str = "",
    price_source: str = "",
    signals: list[dict[str, str]] | None = None,
    used_learnings: list[str] | None = None,
    date_d: str | None = None,
    target_date: str | None = None,
    force: bool = False,
) -> Path:
    """Canonical entry point for writing a D+1 prediction after analysis.

    Every prediction written through this function is guaranteed to use the
    standard path ``predictions/{METAL}_{date_d}_1d.json``, so tomorrow's
    verify phase can find it without guessing.

    Args:
        metal: Metal code, e.g. ``\"CU\"``.
        price_d: Today's spot price used as the prediction baseline.
        predicted_direction: ``\"up\"`` / ``\"down\"`` / ``\"flat\"`` / ``\"volatile\"``.
        predicted_range_pct: ``[low, high]`` percentage range, e.g. ``[0.005, 0.02]``.
        confidence: ``\"high\"`` / ``\"medium\"`` / ``\"low\"``.
        rationale: One-sentence reason for the prediction.
        price_unit: Unit string, e.g. ``\"CNY/ton\"``.
        price_source: Source name, e.g. ``\"SMM\"`` / ``\"100ppi\"``.
        signals: List of signal factors driving this prediction (news, supply-chain,
                 international signals, demand indicators, etc.).
        used_learnings: List of learnings applied to adjust this prediction.
        date_d: Prediction date (default: today).
        target_date: Target date D+1 (default: tomorrow).
        force: If True, overwrite existing prediction for the same metal+date.

    Returns:
        Path to the written prediction file.
    """
    if date_d is None:
        date_d = date.today().isoformat()
    if target_date is None:
        target_date = _add_days(date_d, 1)
    if signals is None:
        signals = []
    if used_learnings is None:
        used_learnings = []
    # Validate required fields
    if not metal or not metal.strip():
        raise ValueError("metal must be a non-empty string")
    if predicted_direction not in ("up", "down", "flat", "volatile"):
        raise ValueError(
            f"predicted_direction must be up/down/flat/volatile, got {predicted_direction!r}"
        )
    if len(predicted_range_pct) != 2:
        raise ValueError("predicted_range_pct must be [low, high]")
    if not rationale or not rationale.strip():
        raise ValueError("rationale must be a non-empty string")
    # Idempotency check
    if not force and prediction_exists(metal, date_d):
        raise FileExistsError(
            f"Prediction already exists for {metal} on {date_d}. "
            f"Use force=True to overwrite."
        )
    record = PredictionRecord(
        metal=metal.strip().upper(),
        date_d=date_d,
        target_date=target_date,
        price_d=price_d,
        price_unit=price_unit,
        price_source=price_source,
        predicted_direction=predicted_direction,
        predicted_range_pct=predicted_range_pct,
        confidence=confidence,
        signals=signals,
        used_learnings=used_learnings,
        rationale=rationale,
        status="pending",
    )
    return write_prediction(record)


def _prediction_to_dict(pred: PredictionRecord) -> dict[str, Any]:
    return {
        "metal": pred.metal,
        "date_d": pred.date_d,
        "target_date": pred.target_date,
        "price_d": pred.price_d,
        "price_unit": pred.price_unit,
        "price_source": pred.price_source,
        "predicted_direction": pred.predicted_direction,
        "predicted_range_pct": pred.predicted_range_pct,
        "confidence": pred.confidence,
        "signals": pred.signals,
        "used_learnings": pred.used_learnings,
        "rationale": pred.rationale,
        "status": pred.status,
    }


def learnings_path(metal: str) -> Path:
    return LEARNINGS_DIR / f"{metal}{LEARNINGS_SUFFIX}"


def load_all_learnings(metals: list[str] | None = None) -> dict[str, list[LearningEntry]]:
    """Parse all {METAL}_learnings.md files. Returns dict keyed by metal code."""
    if metals is None:
        metals = METALS
    all_entries: dict[str, list[LearningEntry]] = {}
    LEARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    for metal in metals:
        p = learnings_path(metal)
        if not p.exists():
            all_entries[metal] = []
            continue
        all_entries[metal] = _parse_learnings_file(p, metal)
    return all_entries


def _parse_learnings_file(path: Path, metal: str) -> list[LearningEntry]:
    """Parse a single learnings.md file into LearningEntry list."""
    entries: list[LearningEntry] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return entries
    # Each entry starts with "## YYYY-MM-DD | METAL | MissType"
    pattern = re.compile(
        r"^##\s+(\d{4}-\d{2}-\d{2})\s*\|\s*(\w+)\s*\|\s*(.+?)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        entry_date = match.group(1)
        entry_metal = match.group(2)
        miss_type = match.group(3).strip()
        # Collect bullet lines until next heading or EOF
        start = match.end()
        end = text.find("\n##", start)
        if end == -1:
            end = len(text)
        body = text[start:end]
        prediction_text = _extract_bullet(body, "Prediction:")
        actual_text = _extract_bullet(body, "Actual:")
        main_reason = _extract_bullet(body, "Main reason:")
        next_adjustment = _extract_bullet(body, "Next adjustment:")
        entries.append(
            LearningEntry(
                date=entry_date,
                metal=entry_metal,
                miss_type=miss_type,
                prediction_text=prediction_text,
                actual_text=actual_text,
                main_reason=main_reason,
                next_adjustment=next_adjustment,
            )
        )
    return entries


def _extract_bullet(text: str, label: str) -> str:
    m = re.search(rf"-\s+{re.escape(label)}\s*(.+)", text)
    return m.group(1).strip() if m else ""


def learning_entry_exists(metal: str, entry_date: str, miss_type: str) -> bool:
    """Check if a learning entry with same date+metal+miss_type already exists."""
    p = learnings_path(metal)
    if not p.exists():
        return False
    # Check if heading already exists
    text = p.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^##\s+{re.escape(entry_date)}\s*\|\s*{re.escape(metal)}\s*\|\s*{re.escape(miss_type)}",
        re.MULTILINE,
    )
    return bool(pattern.search(text))


def append_learning(metal: str, entry: LearningEntry, force: bool = False) -> Path | None:
    """Append a learning entry to {METAL}_learnings.md. Skips if duplicate exists."""
    if not force and learning_entry_exists(metal, entry.date, entry.miss_type):
        return None
    LEARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    p = learnings_path(metal)
    lines = [
        f"## {entry.date} | {entry.metal} | {entry.miss_type}",
        "",
    ]
    if entry.prediction_text:
        lines.append(f"- Prediction: {entry.prediction_text}")
    if entry.actual_text:
        lines.append(f"- Actual: {entry.actual_text}")
    if entry.main_reason:
        lines.append(f"- Main reason: {entry.main_reason}")
    if entry.next_adjustment:
        lines.append(f"- Next adjustment: {entry.next_adjustment}")
    lines.append("")
    block = "\n".join(lines)
    if p.exists():
        current = p.read_text(encoding="utf-8")
        if not current.endswith("\n"):
            current += "\n"
        p.write_text(current + block, encoding="utf-8")
    else:
        p.write_text(block, encoding="utf-8")
    return p


def format_result_as_learning(
    result: ResultRecord,
    prediction: PredictionRecord | None = None,
    bias_reason: str = "",
    next_adjustment: str = "",
) -> LearningEntry:
    """Produce a LearningEntry from a verified ResultRecord.

    Args:
        result: Verified result with hit/miss information.
        prediction: Original prediction (optional — provides price context for the
            Prediction/Actual bullet lines).
        bias_reason: Human-readable explanation for the miss.  In production this
            comes from the LLM agent's bias_review step.
        next_adjustment: Concrete adjustment rule for future predictions.

    Returns:
        A LearningEntry ready for ``append_learning()``.
    """
    miss_type = determine_miss_type(result)
    if not miss_type:
        miss_type = "Verified Hit"
    # Format prediction line with price and range
    if prediction:
        pred_text = (
            f"{prediction.predicted_direction} "
            f"{prediction.predicted_range_pct[0]:+.2%} to "
            f"{prediction.predicted_range_pct[1]:+.2%}"
        )
    else:
        pred_text = f"{result.predicted_direction} [{result.predicted_range_pct[0]:+.2%}, {result.predicted_range_pct[1]:+.2%}]"
    actual_text = (
        f"{result.actual_direction} {result.actual_change_pct:+.2%}"
    )
    return LearningEntry(
        date=result.actual_date,
        metal=result.metal,
        miss_type=miss_type,
        prediction_text=pred_text,
        actual_text=actual_text,
        main_reason=bias_reason,
        next_adjustment=next_adjustment,
    )


def get_recent_learnings_context(
    metal: str,
    limit: int = 5,
) -> str:
    """Read the most recent *limit* learnings for *metal* and return them as
    a compact text block suitable for injecting into a prediction prompt.

    Returns an empty string if no learnings file exists for the metal.
    """
    p = learnings_path(metal)
    if not p.exists():
        return ""
    entries = _parse_learnings_file(p, metal)
    if not entries:
        return ""
    recent = entries[-limit:]
    lines = [f"### Recent Learnings for {metal}"]
    for e in recent:
        lines.append(f"- [{e.date}] {e.miss_type}: {e.main_reason}")
        if e.next_adjustment:
            lines.append(f"  Adjustment: {e.next_adjustment}")
    return "\n".join(lines)


def write_learning_from_result(
    result: ResultRecord,
    prediction: PredictionRecord | None = None,
    bias_reason: str = "",
    next_adjustment: str = "",
    force: bool = False,
) -> Path | None:
    """Convenience: produce a LearningEntry from a result and write it immediately.

    Returns the path to the learnings file, or ``None`` if the entry already
    existed and *force* is ``False``.
    """
    entry = format_result_as_learning(result, prediction, bias_reason, next_adjustment)
    if not entry.main_reason:
        # Don't write empty learnings
        return None
    return append_learning(result.metal, entry, force=force)


def load_json_safe(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def bias_review_output_path(metal: str, prediction_date: str) -> Path:
    return BIAS_REVIEW_DIR / f"{metal}_{prediction_date}.json"


def prediction_output_path(metal: str, today_date: str) -> Path:
    return PREDICTION_OUTPUT_DIR / f"{metal}_{today_date}.json"


def signal_output_path(metal: str, today_date: str) -> Path:
    return SIGNALS_DIR / f"{metal}_{today_date}.json"


def load_bias_review_output(metal: str, prediction_date: str) -> dict[str, str] | None:
    data = load_json_safe(bias_review_output_path(metal, prediction_date))
    if not data:
        return None
    bias_reason = str(data.get("bias_reason", "")).strip()
    next_adjustment = str(data.get("next_adjustment", "")).strip()
    if not bias_reason or not next_adjustment:
        return None
    return {
        "bias_reason": bias_reason,
        "next_adjustment": next_adjustment,
    }


def apply_bias_review_output(
    result: ResultRecord,
    prediction: PredictionRecord | None = None,
    *,
    force: bool = False,
) -> Path | None:
    """Apply external bias_review JSON to a result and append learning.

    The external DeepSeek agent writes
    ``model_outputs/bias_review/{METAL}_{prediction_date}.json``. This function
    is the deterministic handoff point: it validates required fields, updates
    the canonical result JSON, then writes the learning entry.
    """
    if not determine_miss_type(result):
        return None
    payload = load_bias_review_output(result.metal, result.prediction_date)
    if payload is None:
        return None
    result.bias_reason = payload["bias_reason"]
    result.next_adjustment = payload["next_adjustment"]
    write_result(result, force=True)
    return write_learning_from_result(
        result,
        prediction,
        result.bias_reason,
        result.next_adjustment,
        force=force,
    )


def load_signal_output(metal: str, today_date: str) -> list[dict[str, str]]:
    data = load_json_safe(signal_output_path(metal, today_date))
    if data is None:
        return []
    raw = data.get("signals", data)
    return _parse_signal_list(raw)


def load_prediction_output(metal: str, today_date: str) -> dict[str, Any] | None:
    data = load_json_safe(prediction_output_path(metal, today_date))
    if not data:
        return None
    direction = str(data.get("predicted_direction", "")).strip()
    rng = _parse_range(data.get("predicted_range_pct"))
    confidence = str(data.get("confidence", "")).strip()
    rationale = str(data.get("rationale", "")).strip()
    if direction not in ("up", "down", "flat", "volatile"):
        return None
    if not confidence or not rationale:
        return None
    data = dict(data)
    data["predicted_range_pct"] = rng
    data["signals"] = _parse_signal_list(data.get("signals"))
    data["used_learnings"] = _parse_str_list(data.get("used_learnings"))
    return data


def write_prediction_from_model_output(
    metal: str,
    today_date: str,
    price: price_sources.PriceRecord,
    *,
    force: bool = False,
) -> Path | None:
    output = load_prediction_output(metal, today_date)
    if output is None:
        return None
    signals = _parse_signal_list(output.get("signals"))
    if not signals:
        signals = load_signal_output(metal, today_date)
    return record_prediction(
        metal,
        price.price,
        str(output["predicted_direction"]),
        output["predicted_range_pct"],
        str(output["confidence"]),
        str(output["rationale"]),
        price_unit=price.unit,
        price_source=price.source,
        signals=signals,
        used_learnings=output["used_learnings"],
        date_d=today_date,
        target_date=str(output.get("target_date") or _add_days(today_date, 1)),
        force=force,
    )


def write_prediction_from_signals(
    metal: str,
    today_date: str,
    price: price_sources.PriceRecord,
    review: YesterdayReview | None,
    *,
    force: bool = False,
) -> Path | None:
    """Fallback when external agent wrote signals but not final prediction."""
    signals = load_signal_output(metal, today_date)
    if not signals:
        return None
    historical = []
    for entry in load_all_learnings([metal]).get(metal, [])[-5:]:
        if entry.next_adjustment:
            historical.append(entry.next_adjustment)
        elif entry.main_reason:
            historical.append(entry.main_reason)
    by_source: dict[str, list[str]] = {
        "news": [],
        "supply_chain": [],
        "international": [],
        "demand": [],
        "macro": [],
    }
    for sig in signals:
        source = sig.get("source", "")
        text = sig.get("brief") or sig.get("event") or json.dumps(sig, ensure_ascii=False)
        if source in by_source:
            by_source[source].append(text)
        elif source:
            by_source.setdefault(source, []).append(text)
    output = prediction_engine.predict_from_dict(
        {
            "metal": metal,
            "today_date": today_date,
            "spot_price": price.price,
            "price_unit": price.unit,
            "recent_news": by_source.get("news", []),
            "supply_chain_events": by_source.get("supply_chain", []),
            "international_signals": by_source.get("international", []) + by_source.get("macro", []),
            "historical_learnings": historical,
            "yesterday_review": asdict(review) if review else None,
        }
    )
    return record_prediction(
        metal,
        price.price,
        output.predicted_direction,
        output.predicted_range_pct,
        output.confidence,
        output.rationale,
        price_unit=price.unit,
        price_source=price.source,
        signals=signals,
        used_learnings=output.used_learnings,
        date_d=today_date,
        target_date=output.target_date,
        force=force,
    )


# -- Part B: Verification Logic (delegates to backtest_engine) ------------------


def compute_direction(change_pct: float) -> str:
    return backtest_engine.compute_direction(change_pct)


def direction_match(predicted: str, actual: str) -> bool:
    """Check if predicted direction matches actual direction."""
    return predicted == actual


def calculate_hit_result(
    prediction: PredictionRecord,
    actual_price: float,
    actual_source: str = "",
) -> ResultRecord:
    """Pure computation: given a prediction and actual price, produce a ResultRecord.

    Delegates to backtest_engine.verify_prediction for the canonical formula.
    """
    pred_dict = _prediction_to_dict(prediction)
    result_dict = backtest_engine.verify_prediction(
        pred_dict, actual_price, actual_source
    )
    return ResultRecord(
        metal=result_dict["metal"],
        prediction_date=result_dict["prediction_date"],
        actual_date=result_dict["actual_date"],
        price_d=result_dict["price_d"],
        price_d_plus_1=result_dict["price_d_plus_1"],
        actual_change_pct=result_dict["actual_change_pct"],
        predicted_direction=result_dict["predicted_direction"],
        predicted_range_pct=result_dict["predicted_range_pct"],
        actual_direction=result_dict["actual_direction"],
        hit_direction=result_dict["hit_direction"],
        hit_range=result_dict["hit_range"],
        error_pct=result_dict["error_pct"],
        actual_price_source=result_dict["actual_price_source"],
        bias_reason=result_dict["bias_reason"],
        next_adjustment=result_dict.get("next_adjustment", ""),
        status=result_dict["status"],
    )


def determine_miss_type(result: ResultRecord) -> str:
    if result.hit_direction and result.hit_range:
        return ""
    if not result.hit_direction and not result.hit_range:
        return "Missed Direction + Range"
    if not result.hit_direction:
        return "Missed Direction"
    return "Missed Range"


def build_yesterday_review(
    verified: list[ResultRecord],
    pending_metals: list[str],
    today_date: str,
) -> YesterdayReview:
    total = len(verified) + len(pending_metals)
    dir_hits = sum(1 for r in verified if r.hit_direction)
    rng_hits = sum(1 for r in verified if r.hit_range)
    missed = []
    for r in verified:
        mt = determine_miss_type(r)
        if mt:
            missed.append(f"{r.metal}-{_miss_short(mt)}")
    for metal in pending_metals:
        missed.append(f"{metal}-pending")
    if total == 0:
        summary = f"{today_date}: No predictions to review (no predictions found for this date)."
    elif len(pending_metals) == total:
        summary = f"{today_date} Review: {total} predictions pending verification (no actual prices available yet)."
    else:
        parts = [f"{today_date} Review: {len(verified)}/{total} verified"]
        parts.append(f"{dir_hits} direction hits, {rng_hits} range hits")
        if missed:
            parts.append(f"Misses: [{', '.join(missed)}]")
        summary = " — ".join(parts) + "."
    return YesterdayReview(
        date=today_date,
        total_predictions=total,
        verified_count=len(verified),
        pending_count=len(pending_metals),
        hit_direction_count=dir_hits,
        hit_range_count=rng_hits,
        missed_metals=missed,
        summary_text=summary,
    )


def _miss_short(miss_type: str) -> str:
    m = {
        "Missed Direction": "direction",
        "Missed Range": "range",
        "Missed Direction + Range": "both",
    }
    return m.get(miss_type, miss_type.lower())


def format_review_line(review: YesterdayReview) -> str:
    return review.summary_text


def headline_path(date_str: str) -> Path:
    return REPORT_DIR / f"headline_{date_str}.json"


def save_headline(date_str: str, headline: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = headline_path(date_str)
    p.write_text(
        json.dumps({"date": date_str, "headline": headline}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return p


def load_headline(date_str: str) -> str:
    p = headline_path(date_str)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("headline", ""))
    except (json.JSONDecodeError, OSError):
        return ""


def build_headline_context(
    review: YesterdayReview,
    verified: list[ResultRecord],
) -> str:
    """Build a rich context block for LLM headline generation.

    Provides hit-rate stats and per-metal miss details with bias_reason so the
    LLM can write a specific, tonally-appropriate headline.
    """
    total = max(review.verified_count, 1)
    dir_rate = review.hit_direction_count / total
    rng_rate = review.hit_range_count / total

    lines = [
        f"Date: {review.date}",
        f"Verified: {review.verified_count}/{review.total_predictions} predictions",
        f"Direction hit rate: {review.hit_direction_count}/{review.verified_count} ({dir_rate:.0%})",
        f"Range hit rate: {review.hit_range_count}/{review.verified_count} ({rng_rate:.0%})",
    ]

    # Per-metal detail for misses
    missed_details: list[str] = []
    for r in verified:
        mt = determine_miss_type(r)
        if not mt:
            continue
        detail = f"{r.metal}: predicted {r.predicted_direction}, actual {r.actual_direction} ({r.actual_change_pct:+.2%})"
        if r.bias_reason:
            detail += f" — {r.bias_reason}"
        missed_details.append(detail)

    if missed_details:
        lines.append("")
        lines.append("Miss details:")
        for d in missed_details:
            lines.append(f"  - {d}")
    else:
        lines.append("")
        lines.append("All predictions hit both direction and range.")

    return "\n".join(lines)


# -- Part C: Manifest Building -------------------------------------------------


def build_verify_manifest(
    review: YesterdayReview,
    verified: list[ResultRecord],
    pending: list[tuple[Path, PredictionRecord]],
    today_date: str,
    learnings_written: list[str] | None = None,
) -> dict[str, Any]:
    if learnings_written is None:
        learnings_written = []
    tasks: list[dict[str, Any]] = []
    for _, pred in pending:
        low, high = pred.predicted_range_pct
        signals_text = _format_signals_text(pred.signals)
        tasks.append({
            "task_id": f"verify_{pred.metal}_{pred.date_d}",
            "agent": "risk-control",
            "skills_needed": ["spot-price-fetcher", "multi-source-validator", "next-day-volatility-tracker"],
            "action": "fetch_and_verify",
            "metal": pred.metal,
            "prediction_date": pred.date_d,
            "actual_date": pred.target_date,
            "prediction_summary": {
                "price_d": pred.price_d,
                "unit": pred.price_unit,
                "predicted_direction": pred.predicted_direction,
                "predicted_range_pct": pred.predicted_range_pct,
                "rationale": pred.rationale,
            },
            "computation": {
                "formula": "actual_change_pct = (actual_price - price_d) / price_d",
                "direction_rule": f"up if pct>0, down if pct<0, flat if abs(pct)<={FLAT_THRESHOLD}",
                "range_check": f"range hit if {low} <= actual_change_pct <= {high}",
            },
            "instruction": (
                f"Use spot-price-fetcher to get {pred.metal} spot price for {pred.target_date}. "
                f"Compute actual_change_pct = (actual_price - {pred.price_d}) / {pred.price_d}. "
                f"Direction: up if pct>0, down if pct<0, flat if abs(pct)<={FLAT_THRESHOLD}. "
                f"Range hit: {low} <= actual_change_pct <= {high}. "
                f"Write the result to results/{pred.metal}_{pred.date_d}_1d.json.\n\n"
                f"IF MISS (direction or range hit is false), you MUST run bias_review BEFORE writing learnings. "
                f"Follow the bias_review protocol in next-day-volatility-tracker SKILL.md exactly:\n"
                f"  1. Collect D+1 overnight news for {pred.metal} via news-impact-mapper.\n"
                f"  2. Review the original D-day prediction context (signals and rationale below).\n"
                f"  3. Check each category: 突发事件 / 新闻解读偏差 / 市场提前定价 / 宏观反向 / 报价源差异.\n"
                f"  4. Output a JSON object with two fields:\n"
                f'     {{"bias_reason": "1-2句, must name the specific event/indicator/assumption that was wrong", '
                f'"next_adjustment": "1条条件→动作规则"}}\n'
                f"  5. Write bias_reason AND next_adjustment into the result JSON.\n"
                f"  6. Append both to learnings/{pred.metal}_learnings.md.\n\n"
                f"CRITICAL — next_adjustment MUST follow this format:\n"
                f'  "当[触发条件]时，[具体动作]。"\n'
                f"  Choose the best-matching type from the SKILL.md taxonomy:\n"
                f"    区间降档: 当连续上涨超X%/利好已被D日消化时，次日区间降一档。\n"
                f"    区间扩宽: 当重大事件前后波动率突增时，区间扩至[-X%,+X%]。\n"
                f"    方向覆盖: 当特定事件类型必然横盘时，方向改为flat。\n"
                f"    置信度降级: 当仅有单一来源支持时，置信度降一级。\n"
                f"    信号过滤: 当某类信号反复误导时，不再计入。\n"
                f"    暂停预测: 当极端不确定性时，标记为volatile。\n"
                f"  GOOD: '当D日有谈判类利好但夜盘无跟进确认时，次日看涨区间降一档，上限减1%。'\n"
                f"  GOOD: '当美联储议息会议前后24小时，方向改为flat，区间扩至[-1.5%,+1.5%]。'\n"
                f"  BAD: '更谨慎一些。' (no trigger, no action)\n"
                f"  BAD: '注意宏观因素。' (too vague to execute)\n\n"
                f"D-DAY PREDICTION CONTEXT:\n"
                f"  Metal: {pred.metal}\n"
                f"  D-day price: {pred.price_d} {pred.price_unit}\n"
                f"  Predicted: {pred.predicted_direction} [{low:+.2%}, {high:+.2%}]\n"
                f"  Confidence: {pred.confidence}\n"
                f"  Rationale: {pred.rationale}\n"
                f"  D-day signals:\n{signals_text}"
            ),
            "expected_writes": [
                f".workbuddy/memory/backtest/results/{pred.metal}_{pred.date_d}_1d.json",
                str(bias_review_output_path(pred.metal, pred.date_d).relative_to(ROOT)),
                f".workbuddy/memory/backtest/learnings/{pred.metal}_learnings.md (if miss)",
            ],
        })
    for result in verified:
        if not determine_miss_type(result):
            continue
        if result.bias_reason and result.next_adjustment:
            continue
        pred = None
        pred_path = prediction_path(result.metal, result.prediction_date)
        if pred_path.exists():
            try:
                pred = load_prediction(pred_path)
            except Exception:
                pred = None
        signals_text = _format_signals_text(pred.signals) if pred else "  (prediction context unavailable)"
        tasks.append({
            "task_id": f"bias_review_{result.metal}_{result.prediction_date}",
            "agent": "risk-control",
            "skills_needed": ["news-impact-mapper", "next-day-volatility-tracker"],
            "action": "bias_review",
            "metal": result.metal,
            "prediction_date": result.prediction_date,
            "actual_date": result.actual_date,
            "result_summary": result.to_dict(),
            "instruction": (
                f"Run bias_review for {result.metal} prediction {result.prediction_date}. "
                f"The result already missed direction/range, so do not leave bias_reason blank.\n\n"
                f"Original signals:\n{signals_text}\n\n"
                f"Write this exact JSON file: {bias_review_output_path(result.metal, result.prediction_date).relative_to(ROOT)}\n"
                f'{{"bias_reason": "1-2 concrete sentences naming the mistaken event/indicator/assumption", '
                f'"next_adjustment": "When [trigger], [specific action]."}}'
            ),
            "expected_writes": [
                str(bias_review_output_path(result.metal, result.prediction_date).relative_to(ROOT)),
            ],
        })
    # -- Wrap-up task: generate headline after all verifications complete ---------
    headline_ctx = build_headline_context(review, verified)
    tasks.append({
        "task_id": f"headline_{today_date}",
        "agent": "risk-control",
        "skills_needed": [],
        "action": "generate_headline",
        "instruction": (
            f"ALL METAL VERIFICATIONS ARE NOW COMPLETE. Generate a 1-sentence Chinese headline "
            f"summarizing yesterday's prediction accuracy.\n\n"
            f"CONTEXT:\n{headline_ctx}\n\n"
            f"REQUIREMENTS:\n"
            f"  - 1 sentence only, 15-40 Chinese characters.\n"
            f"  - Must mention the overall direction hit rate (e.g. '方向命中率60%').\n"
            f"  - If there are misses, name the key miss reason in plain terms "
            f"(e.g. '煤炭罢工被高估' not '供应扰动预期偏差').\n"
            f"  - If all hit, say so concisely with appropriate satisfaction.\n"
            f"  - Tone: professional but readable, like a veteran analyst's morning note to a colleague.\n"
            f"  - AVOID: empty phrases ('表现尚可'), exaggeration ('惨遭打脸'), "
            f"overly technical jargon ('多因子模型偏离度超阈').\n\n"
            f"OUTPUT: write a JSON file to .workbuddy/memory/reports/headline_{today_date}.json:\n"
            f'  {{"date": "{today_date}", "headline": "你的headline"}}\n\n'
            f"EXAMPLES of good headlines:\n"
            f"  - '昨日方向命中率60%，CU和NI被夜盘宏观逆转拖累，其余3个品种均在区间内。'\n"
            f"  - '昨日3/5命中方向，CO因非洲物流恢复超预期而误判下跌，已记录学习规则。'\n"
            f"  - '昨日全部命中，5个品种方向与区间均在预测范围内，供给端信号持续有效。'"
        ),
        "expected_writes": [
            f".workbuddy/memory/reports/headline_{today_date}.json",
        ],
    })
    context_files = _gather_context_files(pending)
    return {
        "run_id": f"daily-{today_date}-verify",
        "date": today_date,
        "phase": "verify",
        "yesterday_review": asdict(review),
        "verified_results": [r.to_dict() for r in verified],
        "learnings_written": learnings_written,
        "pending_verifications": len(pending),
        "context_files": context_files,
        "tasks": tasks,
        "next_phase": (
            "After all verifications complete, run: "
            f"py scripts\\daily_workbuddy_run.py --phase predict --date {today_date}"
        ),
    }


def build_predict_manifest(
    learnings: dict[str, list[LearningEntry]],
    metals: list[str],
    today_date: str,
    tomorrow_date: str,
) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    for metal in metals:
        if prediction_exists(metal, today_date):
            continue
        recent = learnings.get(metal, [])[-5:]
        learning_context = get_recent_learnings_context(metal, limit=5)
        instruction_parts = [
            f"1. Use spot-price-fetcher to get {metal} spot price for {today_date}.",
            "2. Validate with multi-source-validator if needed.",
            "3. EXTRACT STRUCTURED SIGNALS — scan all sources and output a signals array. "
            "Each signal MUST be a JSON object with these fields:\n"
            '  {"source": "news|supply_chain|international|demand|macro",\n'
            '   "event": "brief description (max 20 chars)",\n'
            '   "direction": "up|down|neutral",\n'
            '   "strength": "high|medium|low",\n'
            '   "timing": "D-day|overnight|ongoing",\n'
            '   "brief": "1-line display summary"}\n'
            "  Sources to scan:\n"
            "    - news-impact-mapper → source='news'\n"
            "    - supply-chain-scanner → source='supply_chain'\n"
            "    - global-price-signal-detector → source='international'\n"
            "    - demand-sensor → source='demand'\n"
            "    - Macro USD/rates/inventory → source='macro'\n"
            "  Strength calibration:\n"
            "    - high: verified by multiple sources OR direct price impact visible\n"
            "    - medium: credible single source with logical causal chain\n"
            "    - low: speculative, rumored, or indirect impact\n"
            "  Output 3-8 signals total across all sources. "
            "These signals will be stored with the prediction and used for bias_review.",
        ]
        if learning_context:
            instruction_parts.append(
                f"4. READ RECENT LEARNINGS BEFORE PREDICTING:\n{learning_context}\n"
                "Apply every Adjustment rule above MECHANICALLY — treat each as a condition→action pair:\n"
                "  - 区间降档: narrow the predicted range by ~1% on the side that was missed.\n"
                "  - 区间扩宽: widen range to the specified bounds.\n"
                "  - 方向覆盖: override predicted direction to the specified value.\n"
                "  - 置信度降级: lower confidence by one notch.\n"
                "  - 信号过滤: exclude the specified signal type from your rationale.\n"
                "  - 暂停预测: set direction=volatile, skip range prediction.\n"
                "If current conditions match a rule's trigger, you MUST apply its action. "
                "Document applied rules in used_learnings."
            )
        step_num = len(instruction_parts) + 1
        instruction_parts.append(
            f"{step_num}. SYNTHESIZE PREDICTION for {tomorrow_date} — use the structured signals from step 3:\n"
            f"  a) Direction: count up vs down signals weighted by strength (high=3, medium=2, low=1).\n"
            f"     - Net score ≥4 → up, ≤-4 → down, otherwise → flat.\n"
            f"     - If any signal has timing='overnight', flag as potential reversal risk.\n"
            f"  b) Range: start from default [-1.5%, +1.5%], then adjust:\n"
            f"     - ≥3 high-strength signals same direction: narrow to [0%, +2%] or [-2%, 0%]\n"
            f"     - Mixed strengths or conflicting: keep default or widen to [-2%, +2%]\n"
            f"     - Any signal with timing='overnight': widen by 0.5% on both sides\n"
            f"     - Apply learning adjustments on top.\n"
            f"  c) Confidence:\n"
            f"     - high: ≥3 signals with strength≥medium agree, no conflicts, no overnight event risk\n"
            f"     - medium: ≥2 signals agree, minor conflicts\n"
            f"     - low: conflicting signals or single-source only\n"
            f"  d) Rationale: 1 sentence. Name the top 2 signals driving the direction.\n"
            f"     Format: '[信号1] + [信号2] → [方向判断], 但[主要风险]。'\n"
            f"  e) used_learnings: list each learning rule description you applied.\n\n"
            f"{step_num + 1}. OUTPUT the prediction as a JSON object:\n"
            f'  {{\n'
            f'    "predicted_direction": "up|down|flat|volatile",\n'
            f'    "predicted_range_pct": [low, high],\n'
            f'    "confidence": "high|medium|low",\n'
            f'    "rationale": "1-sentence synthesis",\n'
            f'    "used_learnings": ["rule description 1", ...]\n'
            f'  }}\n\n'
            f"{step_num + 2}. Write to predictions/{metal}_{today_date}_1d.json via today_predict. "
            f"Include all fields above PLUS the signals array from step 3, "
            f"metal, date_d, target_date, price_d, price_unit, price_source, and status=pending."
        )
        tasks.append({
            "task_id": f"predict_{metal}_{today_date}",
            "agent": "metals-analyst",
            "skills_needed": [
                "spot-price-fetcher", "multi-source-validator",
                "news-impact-mapper", "supply-chain-scanner",
                "global-price-signal-detector", "price-driver-matrix",
                "demand-sensor", "next-day-volatility-tracker",
            ],
            "action": "fetch_and_predict",
            "metal": metal,
            "date_d": today_date,
            "target_date": tomorrow_date,
            "recent_learnings": [
                {
                    "date": e.date,
                    "miss_type": e.miss_type,
                    "prediction": e.prediction_text,
                    "actual": e.actual_text,
                    "main_reason": e.main_reason,
                    "next_adjustment": e.next_adjustment,
                }
                for e in recent
            ],
            "learning_context": learning_context,
            "instruction": "\n".join(instruction_parts),
            "expected_writes": [
                str(prediction_output_path(metal, today_date).relative_to(ROOT)),
                str(signal_output_path(metal, today_date).relative_to(ROOT)),
                f".workbuddy/memory/backtest/predictions/{metal}_{today_date}_1d.json",
            ],
            "required_output_schema": {
                "predicted_direction": "up|down|flat|volatile",
                "predicted_range_pct": "[low, high]",
                "confidence": "high|medium|low",
                "rationale": "1-sentence synthesis",
                "used_learnings": ["rule description..."],
            },
        })
    context_files: list[str] = []
    for metal in metals:
        p = learnings_path(metal)
        if p.exists():
            context_files.append(str(p.relative_to(ROOT)))
    return {
        "run_id": f"daily-{today_date}-predict",
        "date": today_date,
        "phase": "predict",
        "tomorrow_date": tomorrow_date,
        "metals_to_predict": len(tasks),
        "already_predicted": sum(1 for m in metals if prediction_exists(m, today_date)),
        "context_files": context_files,
        "tasks": tasks,
        "next_phase": (
            "After all predictions complete, run: "
            f"py scripts\\daily_workbuddy_run.py --phase report --date {today_date}"
        ),
    }


def _gather_context_files(pending: list[tuple[Path, PredictionRecord]]) -> list[str]:
    files: list[str] = []
    for path, _ in pending:
        files.append(str(path.relative_to(ROOT)))
    return files


# -- Part D: Report Generation -------------------------------------------------


def assemble_daily_report_json(
    today_date: str,
    todays_predictions: list[PredictionRecord],
    verified_results: list[ResultRecord],
    all_learnings: dict[str, list[LearningEntry]],
    review: YesterdayReview,
    learnings_written: list[str] | None = None,
) -> dict[str, Any]:
    if learnings_written is None:
        learnings_written = []
    metals_section: list[dict[str, Any]] = []
    for pred in todays_predictions:
        metals_section.append({
            "code": pred.metal,
            "name": _metal_name(pred.metal),
            "spot_price": pred.price_d,
            "unit": pred.price_unit,
            "source": pred.price_source,
            "direction": pred.predicted_direction,
            "range_pct": pred.predicted_range_pct,
            "confidence": pred.confidence,
            "rationale": pred.rationale,
        })

    backtests_section: list[dict[str, Any]] = []
    for r in verified_results:
        backtests_section.append({
            "metal": r.metal,
            "prediction_date": r.prediction_date,
            "actual_date": r.actual_date,
            "predicted_direction": r.predicted_direction,
            "actual_direction": r.actual_direction,
            "hit_direction": r.hit_direction,
            "hit_range": r.hit_range,
            "bias_reason": r.bias_reason,
            "next_adjustment": r.next_adjustment,
        })

    sections: list[dict[str, Any]] = [
        {
            "title": "昨日预测复盘",
            "body": review.summary_text,
            "columns": [
                {"key": "metal", "label": "Metal"},
                {"key": "prediction_date", "label": "Prediction Date"},
                {"key": "actual_date", "label": "Actual Date"},
                {"key": "predicted_direction", "label": "Predicted"},
                {"key": "actual_direction", "label": "Actual"},
                {"key": "hit_direction", "label": "Direction Hit"},
                {"key": "hit_range", "label": "Range Hit"},
                {"key": "bias_reason", "label": "Bias Reason"},
                {"key": "next_adjustment", "label": "Next Adjustment"},
            ],
            "rows": backtests_section,
        },
    ]

    # Add recent learnings as a section
    learning_items: list[str] = []
    for metal, entries in all_learnings.items():
        for e in entries[-2:]:
            learning_items.append(
                f"[{e.date}] {e.metal} - {e.miss_type}: {e.main_reason}"
            )
    if learning_items:
        sections.append({
            "title": "近期偏差经验",
            "items": learning_items[-10:],
        })

    direction_hits = review.hit_direction_count
    range_hits = review.hit_range_count
    verified_count = review.verified_count

    # Prefer LLM-generated headline; fall back to mechanical summary
    generated_headline = load_headline(today_date)
    if generated_headline:
        yesterday_headline = generated_headline
    elif review.total_predictions == 0 and not backtests_section:
        yesterday_headline = "昨天没有登记的一日预测，今天先建立基准预测记录"
    else:
        yesterday_headline = review.summary_text

    yesterday_review = {
        "headline": yesterday_headline,
        "summary": (
            f"昨日共{review.total_predictions}笔预测，已验证{verified_count}笔。"
            f"方向命中{direction_hits}/{max(verified_count, 1)}，"
            f"区间命中{range_hits}/{max(verified_count, 1)}。"
            + (f" 未命中: {', '.join(review.missed_metals)}。" if review.missed_metals else "")
        ),
        "backtests": backtests_section,
        "learnings_written": learnings_written,
    }

    return {
        "report_id": f"daily_briefing_{today_date}",
        "report_type": "daily_briefing",
        "date": today_date,
        "title": f"刀具原材料行情早报 {today_date}",
        "summary": (
            f"今日覆盖{len(metals_section)}种金属的D+1波动预测。"
            f"昨日复盘: {verified_count}笔预测已验证, "
            f"方向命中率{direction_hits}/{verified_count if verified_count else 1}, "
            f"区间命中率{range_hits}/{verified_count if verified_count else 1}。"
        ),
        "metals": metals_section,
        "backtests": backtests_section,
        "yesterday_review": yesterday_review,
        "sections": sections,
        "recommendation": {
            "action": "参见各金属策略速览",
            "reason": review.summary_text,
            "risk_level": "medium",
        },
        "json_payload": {
            "yesterday_backtest": {
                "date": today_date,
                "status": "verified" if verified_count > 0 else "pending",
                "total": review.total_predictions,
                "direction_hits": direction_hits,
                "range_hits": range_hits,
            },
            "backtests": backtests_section,
        },
    }


def generate_and_publish_report(
    report_dict: dict[str, Any],
    auto_open_html: bool | None = None,
) -> dict[str, Any]:
    """Write report JSON, call build_report to generate HTML, then rebuild index."""
    import build_html_report
    cfg = get_config().get("report", {})
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"{report_dict['report_id']}.json"
    json_path.write_text(
        json.dumps(report_dict, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_path, latest_json_path, snapshot_path = build_html_report.build_report(
        json_path
    )
    index_path = build_html_report.build_index()
    sidecar_path = None
    workbuddy_index_paths: list[Path] = []
    workbuddy_root = Path(str(cfg.get("workbuddy_root") or Path.home() / "WorkBuddy")).expanduser()
    if cfg.get("publish_workbuddy_sidecar", True) and workbuddy_root.exists():
        sidecar_path = build_html_report.write_workbuddy_visible_report(
            json_path,
            report_path,
            workbuddy_root=workbuddy_root,
        )
    if cfg.get("refresh_index", True):
        if workbuddy_root.exists():
            try:
                workbuddy_index_paths = build_html_report.build_workbuddy_indexes(workbuddy_root)
            except OSError:
                workbuddy_index_paths = []

    open_target = sidecar_path or report_path
    opened_html = False
    open_error = ""
    should_open_html = cfg.get("auto_open_html", True) if auto_open_html is None else auto_open_html
    if should_open_html:
        opened_html, open_error = build_html_report.open_html_report(open_target)

    chat_panel_files = [
        {
            "label": "主报告 HTML",
            "path": str(report_path.resolve()),
            "mime_type": "text/html",
            "display": "open",
        },
        {
            "label": "报告中心索引",
            "path": str(index_path.resolve()),
            "mime_type": "text/html",
            "display": "open",
        },
    ]
    if sidecar_path is not None:
        chat_panel_files.insert(
            0,
            {
                "label": "WorkBuddy 面板副本",
                "path": str(sidecar_path.resolve()),
                "mime_type": "text/html",
                "display": "open",
            },
        )
    if workbuddy_index_paths:
        chat_panel_files.append(
            {
                "label": "WorkBuddy 报告索引",
                "path": str((workbuddy_root / "index.html").resolve()),
                "mime_type": "text/html",
                "display": "open",
            }
        )

    return {
        "json_path": json_path,
        "report_path": report_path,
        "latest_json_path": latest_json_path,
        "snapshot_path": snapshot_path,
        "index_path": index_path,
        "sidecar_path": sidecar_path,
        "workbuddy_index_paths": workbuddy_index_paths,
        "opened_html": opened_html,
        "open_error": open_error,
        "chat_panel_files": chat_panel_files,
    }


_METAL_NAMES: dict[str, str] = {
    "CO": "钴 / Cobalt",
    "W": "钨 / Tungsten",
    "NI": "镍 / Nickel",
    "IRON_ORE": "铁矿石 / Iron Ore",
    "CU": "铜 / Copper",
    "AL": "铝 / Aluminum",
    "ZN": "锌 / Zinc",
    "SN": "锡 / Tin",
    "PB": "铅 / Lead",
}


def _metal_name(code: str) -> str:
    return _METAL_NAMES.get(code.upper(), code)


# -- Part E: Phase Runners -----------------------------------------------------


def run_verify_phase(
    today_date: str,
    force: bool = False,
) -> tuple[str, dict[str, Any]]:
    ensure_directories()
    yesterdays = find_yesterdays_predictions(today_date)
    verified: list[ResultRecord] = []
    pending: list[tuple[Path, PredictionRecord]] = []
    learnings_written: list[str] = []
    for path, pred in yesterdays:
        existing = load_existing_result(pred.metal, pred.date_d)
        if existing and not force:
            learning_path = apply_bias_review_output(existing, pred, force=force)
            if (
                learning_path is None
                and determine_miss_type(existing)
                and existing.bias_reason
                and existing.next_adjustment
            ):
                learning_path = write_learning_from_result(
                    existing,
                    pred,
                    existing.bias_reason,
                    existing.next_adjustment,
                    force=force,
                )
            if learning_path is not None:
                learnings_written.append(str(learning_path.relative_to(ROOT)))
                existing = load_existing_result(pred.metal, pred.date_d) or existing
            verified.append(existing)
        else:
            actual_price = price_sources.get_spot_price(pred.metal, pred.target_date)
            if actual_price.status == "available":
                result = calculate_hit_result(
                    pred,
                    actual_price.price,
                    actual_price.source,
                )
                applied_learning_path = apply_bias_review_output(result, pred, force=force)
                write_result(result, force=force)
                learning_path = applied_learning_path
                if (
                    learning_path is None
                    and determine_miss_type(result)
                    and result.bias_reason
                    and result.next_adjustment
                ):
                    learning_path = write_learning_from_result(
                        result,
                        pred,
                        result.bias_reason,
                        result.next_adjustment,
                        force=force,
                    )
                if learning_path is not None:
                    learnings_written.append(str(learning_path.relative_to(ROOT)))
                verified.append(result)
            else:
                pending.append((path, pred))
    # Also load results that exist independently of predictions
    if not yesterdays:
        for path in sorted(RESULTS_DIR.glob(f"*{PRED_SUFFIX}")):
            try:
                r = load_result(path)
                if r.actual_date == today_date:
                    verified.append(r)
            except Exception:
                pass
    pending_metals = [pred.metal for _, pred in pending]
    review = build_yesterday_review(verified, pending_metals, today_date)
    manifest = build_verify_manifest(
        review, verified, pending, today_date, learnings_written
    )
    return format_review_line(review), manifest


def run_predict_phase(
    today_date: str,
    metals: list[str] | None = None,
    force: bool = False,
) -> tuple[str, dict[str, Any]]:
    if metals is None:
        metals = METALS
    ensure_directories()
    learnings = load_all_learnings(metals)
    # Build a quick review line from whatever results are available
    verified = _load_all_verified_for_date(today_date)
    if verified:
        dir_hits = sum(1 for r in verified if r.hit_direction)
        rng_hits = sum(1 for r in verified if r.hit_range)
        review_line = (
            f"{today_date} Review: {len(verified)} verified — "
            f"{dir_hits} direction hits, {rng_hits} range hits."
        )
    else:
        review_line = f"{today_date}: Preparing predictions — no verified results available yet."
    tomorrow = _add_days(today_date, 1)
    review = build_yesterday_review(verified, [], today_date)
    predictions_written: list[str] = []
    for metal in metals:
        if prediction_exists(metal, today_date) and not force:
            continue
        price = price_sources.get_spot_price(metal, today_date)
        if price.status != "available":
            continue
        written = write_prediction_from_model_output(
            metal,
            today_date,
            price,
            force=force,
        )
        if written is None:
            written = write_prediction_from_signals(
                metal,
                today_date,
                price,
                review,
                force=force,
            )
        if written is not None:
            predictions_written.append(str(written.relative_to(ROOT)))
    manifest = build_predict_manifest(learnings, metals, today_date, tomorrow)
    manifest["predictions_written"] = predictions_written
    return review_line, manifest


def run_report_phase(
    today_date: str,
    metals: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    if metals is None:
        metals = METALS
    ensure_directories()
    todays_preds = [pred for _, pred in find_todays_predictions(today_date)]
    verified = _load_all_verified_for_date(today_date)
    learnings = load_all_learnings(metals)
    pending_metals: list[str] = []
    review = build_yesterday_review(verified, pending_metals, today_date)
    review_line = format_review_line(review)
    report_dict = assemble_daily_report_json(
        today_date, todays_preds, verified, learnings, review
    )
    published = generate_and_publish_report(
        report_dict
    )
    return review_line, {
        "phase": "report",
        "date": today_date,
        "review": asdict(review),
        "report_json": str(published["json_path"].resolve()),
        "report_html": str(published["report_path"].resolve()),
        "index_html": str(published["index_path"].resolve()),
        "workbuddy_report_html": (
            str(published["sidecar_path"].resolve()) if published["sidecar_path"] else ""
        ),
        "opened_html": published["opened_html"],
        "open_error": published["open_error"],
        "chat_panel_files": published["chat_panel_files"],
        "predictions_included": len(todays_preds),
        "backtests_included": len(verified),
    }


def run_full(
    today_date: str,
    metals: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Default (no --phase): run verify + predict manifests combined."""
    if metals is None:
        metals = METALS
    review_line, verify_manifest = run_verify_phase(today_date)
    _, predict_manifest = run_predict_phase(today_date, metals)
    combined = {
        "date": today_date,
        "verify": verify_manifest,
        "predict": predict_manifest,
    }
    return review_line, combined


def _load_all_verified_for_date(actual_date: str) -> list[ResultRecord]:
    verified: list[ResultRecord] = []
    if not RESULTS_DIR.exists():
        return verified
    for path in sorted(RESULTS_DIR.glob(f"*{PRED_SUFFIX}")):
        try:
            r = load_result(path)
            if r.actual_date == actual_date:
                verified.append(r)
        except Exception:
            pass
    return verified


def _add_days(date_str: str, days: int) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (d + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return date_str


# -- CLI -----------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--phase",
        choices=["verify", "predict", "report"],
        default=None,
        help="Which phase to run. Default: run verify + predict manifests combined.",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=date.today().isoformat(),
        help="Target date YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--metals",
        type=str,
        default=None,
        help="Comma-separated metal codes (default: all 9). Example: --metals CU,CO,W",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Output ONLY the JSON manifest (suppress review line).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow overwriting existing result/prediction files.",
    )
    args = parser.parse_args()

    today_date = args.date
    metals = None
    if args.metals:
        metals = [m.strip().upper() for m in args.metals.split(",") if m.strip()]

    try:
        if args.phase == "verify":
            review_line, manifest = run_verify_phase(today_date, force=args.force)
        elif args.phase == "predict":
            review_line, manifest = run_predict_phase(today_date, metals=metals, force=args.force)
        elif args.phase == "report":
            review_line, manifest = run_report_phase(
                today_date, metals=metals
            )
        else:
            review_line, manifest = run_full(today_date, metals=metals)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not args.json_only:
        print(review_line)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
