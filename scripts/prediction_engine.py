#!/usr/bin/env python3
"""Prediction engine — rule-based D+1 metal price direction and range forecast.

Core responsibilities:
  1. Collect and normalise inputs (spot price, news, supply chain, signals,
     historical learnings, yesterday's review).
  2. Match historical learnings to the current situation via keyword rules.
  3. Apply rule-based heuristics to produce a base prediction.
  4. Adjust the prediction using matched learnings.
  5. Output a structured PredictionOutput with used_learnings explicitly
     surfaced so the Agent can explain *why* it predicted what it did.

This is deliberately rule-based, not an ML model.  The goal is to make
"偏差沉淀" (bias sedimentation) visible — every prediction cites which past
lessons it applied.
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

ROOT = Path(__file__).resolve().parents[1]
LEARNINGS_DIR = ROOT / ".workbuddy" / "memory" / "learnings"
PREDICTIONS_DIR = ROOT / ".workbuddy" / "memory" / "backtest" / "predictions"

FLAT_THRESHOLD = 0.005
DEFAULT_RANGE_PCT = [-0.015, 0.015]

METAL_NAMES: dict[str, str] = {
    "CO": "Co", "W": "W", "NI": "Ni", "IRON_ORE": "Iron Ore",
    "CU": "Cu", "AL": "Al", "ZN": "Zn", "SN": "Sn", "PB": "Pb",
}

DIRECTION_RANK = {"down": -2, "flat": 0, "volatile": 0, "up": 2}


# -- Data structures ------------------------------------------------------------

@dataclass
class PredictionInput:
    """All signals the engine considers for a single metal on a single day."""

    metal: str
    today_date: str
    spot_price: float
    price_unit: str = ""
    recent_news: list[str] = field(default_factory=list)
    supply_chain_events: list[str] = field(default_factory=list)
    international_signals: list[str] = field(default_factory=list)
    historical_learnings: list[str] = field(default_factory=list)
    yesterday_review: dict[str, Any] | None = None
    prior_direction: str = ""
    prior_range_pct: list[float] = field(default_factory=lambda: [-0.015, 0.015])
    consecutive_up_days: int = 0
    price_5d_ago: float | None = None


@dataclass
class PredictionOutput:
    """Structured forecast for a single metal on a single day."""

    metal: str
    today_date: str
    target_date: str
    spot_price: float
    price_unit: str
    predicted_direction: str
    predicted_range_pct: list[float]
    confidence: str
    rationale: str
    used_learnings: list[str] = field(default_factory=list)
    base_direction: str = ""
    adjustments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- Learning matching engine ---------------------------------------------------

LEARNING_RULES: list[dict[str, Any]] = [
    {
        "keywords": ["谈判", "达成", "协议", "路线图"],
        "context_news_has": ["谈判", "协议", "达成一致", "路线图"],
        "effect": "downgrade_bullish",
        "description": "谈判类事件如果已被市场提前交易，次日涨幅预测降一档",
    },
    {
        "keywords": ["连续上涨", "利好兑现", "利好新闻"],
        "context_news_has": ["利好", "上涨"],
        "context_price_has": "consecutive_up",
        "effect": "check_priced_in",
        "description": "连续上涨后遇到利好新闻，优先检查是否属于利好兑现",
    },
    {
        "keywords": ["政策", "生效", "立法窗", "信息密集"],
        "context_news_has": ["政策", "生效", "立法", "法规"],
        "effect": "reduce_confidence",
        "description": "政策/法规生效日不确定性极高，置信度自动降一级",
    },
    {
        "keywords": ["FOMC", "央行", "利率决议", "过度定价", "卖事实"],
        "context_signals_has": ["FOMC", "央行", "利率", "过度定价"],
        "effect": "direction_to_neutral",
        "description": "FOMC过度定价时宏观敏感金属方向转中性，警惕卖事实反弹",
    },
    {
        "keywords": ["矿山", "停产", "出口管制", "配额", "供应链中断"],
        "context_events_has": ["停产", "出口管制", "配额", "中断", "矿山"],
        "effect": "boost_bullish",
        "description": "供应链中断/矿山停产类事件，短期内方向偏强",
    },
    {
        "keywords": ["季节性", "淡季", "旺季"],
        "context_news_has": ["季节", "淡季", "旺季"],
        "effect": "seasonal_bias",
        "description": "季节性因素需结合历史同期方向偏差修正",
    },
    {
        "keywords": ["仓单", "库存", "LME", "SHFE", "累库", "去库"],
        "context_signals_has": ["仓单", "库存", "LME", "SHFE"],
        "effect": "inventory_signal",
        "description": "交易所仓单/库存变化可作为短期方向辅助信号",
    },
    {
        "keywords": ["内外价差", "套利窗口", "进口盈亏"],
        "context_signals_has": ["价差", "套利", "进口盈亏"],
        "effect": "arbitrage_signal",
        "description": "内外价差/套利窗口打开时，国内价格向进口平价回归",
    },
]


def _text_contains_any(text: str, keywords: list[str]) -> bool:
    """True if *text* contains at least one of *keywords*."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _texts_contain_any(texts: list[str], keywords: list[str]) -> bool:
    """True if any text in *texts* contains at least one keyword."""
    return any(_text_contains_any(t, keywords) for t in texts)


def match_learnings(inp: PredictionInput) -> list[dict[str, Any]]:
    """Return learning rules whose trigger conditions match the current *inp*."""
    matched: list[dict[str, Any]] = []
    for rule in LEARNING_RULES:
        kw = rule.get("keywords", [])
        if not kw:
            continue
        learnings_text = " ".join(inp.historical_learnings)
        if not _text_contains_any(learnings_text, kw):
            continue
        ctx_news = rule.get("context_news_has")
        if ctx_news and not _texts_contain_any(inp.recent_news, ctx_news):
            continue
        ctx_events = rule.get("context_events_has")
        if ctx_events and not _texts_contain_any(inp.supply_chain_events, ctx_events):
            continue
        ctx_signals = rule.get("context_signals_has")
        if ctx_signals and not _texts_contain_any(inp.international_signals, ctx_signals):
            continue
        ctx_price = rule.get("context_price_has")
        if ctx_price == "consecutive_up" and inp.consecutive_up_days < 3:
            continue
        matched.append(rule)
    return matched


# -- Rule-based prediction -------------------------------------------------------

def _news_sentiment(news: list[str]) -> int:
    """Crude sentiment: +1 bullish, -1 bearish, 0 neutral."""
    bullish = ["涨", "上涨", "利好", "收紧", "短缺", "停产", "制裁", "突破", "反弹"]
    bearish = ["跌", "下跌", "利空", "过剩", "增产", "放开", "抛储", "需求疲软", "衰退"]
    score = 0
    for item in news:
        score += sum(1 for w in bullish if w in item)
        score -= sum(1 for w in bearish if w in item)
    return 1 if score > 0 else (-1 if score < 0 else 0)


def _events_sentiment(events: list[str]) -> int:
    """Supply-chain events: disruption -> bullish, resolution -> bearish."""
    bullish = ["停产", "管制", "收紧", "配额", "中断", "制裁", "事故", "罢工"]
    bearish = ["复产", "放开", "增产", "解除", "恢复", "新矿", "扩产"]
    score = 0
    for item in events:
        score += sum(1 for w in bullish if w in item)
        score -= sum(1 for w in bearish if w in item)
    return 1 if score > 0 else (-1 if score < 0 else 0)


def compute_base_prediction(inp: PredictionInput) -> tuple[str, list[float], str, list[str]]:
    """Compute a base prediction before learning adjustments.

    Returns:
        (direction, range_pct, confidence, reason_parts)
    """
    reasons: list[str] = []
    news_sent = _news_sentiment(inp.recent_news)
    event_sent = _events_sentiment(inp.supply_chain_events)

    if news_sent > 0:
        reasons.append("新闻面偏多")
    elif news_sent < 0:
        reasons.append("新闻面偏空")
    else:
        reasons.append("新闻面中性")

    if event_sent > 0:
        reasons.append("供给收缩信号")
    elif event_sent < 0:
        reasons.append("供给宽松信号")

    sig_bullish = _texts_contain_any(
        inp.international_signals,
        ["backwardation", "升水", "溢价", "套利窗口", "现货升水"],
    )
    sig_bearish = _texts_contain_any(
        inp.international_signals,
        ["contango", "贴水", "折价", "过剩"],
    )
    if sig_bullish:
        reasons.append("国际价格信号偏强")
    if sig_bearish:
        reasons.append("国际价格信号偏弱")

    combined = news_sent + event_sent + (1 if sig_bullish else 0) + (-1 if sig_bearish else 0)

    if combined >= 2:
        direction = "up"
        conf = "medium"
    elif combined <= -2:
        direction = "down"
        conf = "medium"
    elif combined == 1:
        direction = "up"
        conf = "low"
    elif combined == -1:
        direction = "down"
        conf = "low"
    else:
        direction = "flat"
        conf = "low"

    rng = DEFAULT_RANGE_PCT.copy()
    if direction == "up":
        rng = [-0.005, 0.020]
    elif direction == "down":
        rng = [-0.020, 0.005]

    reasons.insert(0, f"综合信号得分={combined:+d}")
    return direction, rng, conf, reasons


# -- Learning adjustment ---------------------------------------------------------

def apply_learnings(
    direction: str,
    range_pct: list[float],
    confidence: str,
    reasons: list[str],
    matched: list[dict[str, Any]],
    inp: PredictionInput,
) -> tuple[str, list[float], str, list[str], list[str]]:
    """Adjust a base prediction using matched learning rules.

    Returns:
        (direction, range_pct, confidence, reasons, used_learning_descriptions)
    """
    used: list[str] = []
    adj: list[str] = []

    for rule in matched:
        effect = rule.get("effect", "")
        desc = rule.get("description", "")
        used.append(desc)

        if effect == "downgrade_bullish" and direction == "up":
            direction = "flat"
            confidence = "low"
            adj.append(f"应用「{desc}」: 看涨降为震荡")
        elif effect == "check_priced_in":
            if inp.consecutive_up_days >= 3:
                confidence = "low"
                adj.append(f"应用「{desc}」: 连续上涨+利好，降置信度")
        elif effect == "reduce_confidence":
            conf_rank = {"high": "medium", "medium": "low", "low": "low"}
            confidence = conf_rank.get(confidence, confidence)
            adj.append(f"应用「{desc}」: 置信度降一级")
        elif effect == "direction_to_neutral":
            if direction in ("down", "up"):
                direction = "flat"
                confidence = "low"
                adj.append(f"应用「{desc}」: 方向转中性")
        elif effect == "boost_bullish":
            if direction == "flat":
                direction = "up"
                confidence = "low"
            elif direction == "up":
                confidence = "medium"
            adj.append(f"应用「{desc}」: 供给收缩加强看涨")
        elif effect == "arbitrage_signal":
            adj.append(f"应用「{desc}」: 关注价差回归方向")

    if adj:
        reasons.append("学习规则调整: " + "; ".join(adj))

    return direction, range_pct, confidence, reasons, used


# -- Main entry point ------------------------------------------------------------

def predict(inp: PredictionInput) -> PredictionOutput:
    """Run the full prediction pipeline: base -> learnings -> output."""
    base_dir, rng, conf, reasons = compute_base_prediction(inp)

    yesterday = inp.yesterday_review
    if yesterday and isinstance(yesterday, dict):
        yr_backtests = yesterday.get("backtests", [])
        if yr_backtests:
            yr_hits = sum(1 for bt in yr_backtests if bt.get("hit_direction"))
            reasons.append(
                f"昨日复盘: 方向命中{yr_hits}/{len(yr_backtests)}"
            )

    matched = match_learnings(inp)
    direction, rng, conf, reasons, used = apply_learnings(
        base_dir, rng, conf, reasons, matched, inp
    )

    target = _add_days(inp.today_date, 1)
    rationale = "; ".join(reasons)

    return PredictionOutput(
        metal=inp.metal,
        today_date=inp.today_date,
        target_date=target,
        spot_price=inp.spot_price,
        price_unit=inp.price_unit,
        predicted_direction=direction,
        predicted_range_pct=rng,
        confidence=conf,
        rationale=rationale,
        used_learnings=used,
        base_direction=base_dir,
        adjustments=[],
    )


def predict_from_dict(data: dict[str, Any]) -> PredictionOutput:
    """Build a PredictionInput from a raw dict and run predict()."""
    inp = PredictionInput(
        metal=str(data.get("metal", "")),
        today_date=str(data.get("today_date", date.today().isoformat())),
        spot_price=float(data.get("spot_price", 0)),
        price_unit=str(data.get("price_unit", "")),
        recent_news=_str_list(data.get("recent_news")),
        supply_chain_events=_str_list(data.get("supply_chain_events")),
        international_signals=_str_list(data.get("international_signals")),
        historical_learnings=_str_list(data.get("historical_learnings")),
        yesterday_review=data.get("yesterday_review"),
        prior_direction=str(data.get("prior_direction", "")),
        prior_range_pct=_float_list(data.get("prior_range_pct"), DEFAULT_RANGE_PCT),
        consecutive_up_days=int(data.get("consecutive_up_days", 0)),
        price_5d_ago=_optional_float(data.get("price_5d_ago")),
    )
    return predict(inp)


# -- Persistence ----------------------------------------------------------------

def save_prediction(output: PredictionOutput) -> Path:
    """Persist a prediction record for backtest tracking."""
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = PREDICTIONS_DIR / f"{output.metal}_{output.today_date}_1d.json"
    record = {
        "metal": output.metal,
        "date_d": output.today_date,
        "target_date": output.target_date,
        "price_d": output.spot_price,
        "predicted_direction": output.predicted_direction,
        "predicted_range_pct": output.predicted_range_pct,
        "confidence": output.confidence,
        "rationale": output.rationale,
        "used_learnings": output.used_learnings,
    }
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def save_learnings(learnings: list[str]) -> Path:
    """Append learnings to the persistent learnings file."""
    LEARNINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LEARNINGS_DIR / "learnings.json"
    existing: list[str] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    seen = set(existing)
    new_items = [l for l in learnings if l not in seen]
    if new_items:
        existing.extend(new_items)
        path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return path


def load_learnings() -> list[str]:
    """Load all historical learnings."""
    path = LEARNINGS_DIR / "learnings.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


# -- Helpers --------------------------------------------------------------------

def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []


def _float_list(value: Any, default: list[float]) -> list[float]:
    if isinstance(value, list) and len(value) >= 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            pass
    return default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _add_days(date_str: str, days: int) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (d + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return date_str


# -- CLI ------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prediction engine — rule-based D+1 metal forecast with learning matching.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--metal", type=str, required=True, help="Metal code, e.g. AL, CO, CU.")
    parser.add_argument("--spot-price", type=float, required=True, help="Today's spot price.")
    parser.add_argument("--date", type=str, default=date.today().isoformat(), help="Today's date YYYY-MM-DD.")
    parser.add_argument("--price-unit", type=str, default="", help="Price unit, e.g. CNY/ton.")
    parser.add_argument("--news-file", type=str, default=None, help="JSON file with a 'news' array.")
    parser.add_argument("--events-file", type=str, default=None, help="JSON file with an 'events' array.")
    parser.add_argument("--signals-file", type=str, default=None, help="JSON file with a 'signals' array.")
    parser.add_argument("--review-file", type=str, default=None, help="JSON file with yesterday_review object.")
    parser.add_argument("--learnings-file", type=str, default=None, help="JSON file with a 'learnings' array.")
    parser.add_argument("--consecutive-up-days", type=int, default=0)
    parser.add_argument("--price-5d-ago", type=float, default=None)
    parser.add_argument("--json-only", action="store_true", help="Output only JSON, suppress summary.")
    parser.add_argument("--save", action="store_true", help="Persist prediction record for backtest tracking.")
    parser.add_argument("--save-learnings", action="store_true", help="Persist new learnings to the store.")
    args = parser.parse_args()

    def _load_json_array(path_str: str | None, key: str) -> list[str]:
        if not path_str:
            return []
        try:
            data = json.loads(Path(path_str).read_text(encoding="utf-8"))
            return _str_list(data.get(key, []))
        except Exception:
            return []

    inp = PredictionInput(
        metal=args.metal.upper(),
        today_date=args.date,
        spot_price=args.spot_price,
        price_unit=args.price_unit,
        recent_news=_load_json_array(args.news_file, "news"),
        supply_chain_events=_load_json_array(args.events_file, "events"),
        international_signals=_load_json_array(args.signals_file, "signals"),
        historical_learnings=_load_json_array(args.learnings_file, "learnings") or load_learnings(),
        yesterday_review=_load_review(args.review_file),
        consecutive_up_days=args.consecutive_up_days,
        price_5d_ago=args.price_5d_ago,
    )

    output = predict(inp)

    if not args.json_only:
        print(
            f"{output.today_date} -> {output.target_date} | {output.metal}: "
            f"predicted {output.predicted_direction} "
            f"range {output.predicted_range_pct} "
            f"confidence={output.confidence}"
        )
        if output.used_learnings:
            print(f"Used learnings ({len(output.used_learnings)}):")
            for ul in output.used_learnings:
                print(f"  - {ul}")
        print(f"Rationale: {output.rationale}")

    print(json.dumps(output.to_dict(), ensure_ascii=False, indent=2))

    if args.save:
        pred_path = save_prediction(output)
        if not args.json_only:
            print(f"Saved prediction: {pred_path}", file=sys.stderr)

    if args.save_learnings and output.used_learnings:
        learn_path = save_learnings(output.used_learnings)
        if not args.json_only:
            print(f"Saved learnings: {learn_path}", file=sys.stderr)

    return 0


def _load_review(path_str: str | None) -> dict[str, Any] | None:
    if not path_str:
        return None
    try:
        return json.loads(Path(path_str).read_text(encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())