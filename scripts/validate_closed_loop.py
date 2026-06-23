#!/usr/bin/env python3
"""Local smoke checks for the WorkBuddy closed-loop daily workflow.

Run from the repository root:

    py -B scripts\validate_closed_loop.py

The checks patch module paths into temporary directories and do not touch the
real .workbuddy state.
"""

from __future__ import annotations

import html
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_html_report
import daily_workbuddy_run as daily
import prediction_engine
import price_sources


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        original = _patch_paths(root)
        try:
            _check_iron_ore_cache_parse()
            _check_verify_writes_result_and_learning()
            _check_external_bias_review_is_applied()
            _check_external_prediction_is_consumed()
            _check_signals_fallback_prediction()
            _check_first_day_report_generation()
            _check_yesterday_review_first_block()
        finally:
            _restore_paths(original)
    print("closed-loop validation passed")
    return 0


def _patch_paths(root: Path) -> dict[str, Any]:
    backtest = daily.backtest_engine
    original = {
        "daily_ROOT": daily.ROOT,
        "price_ROOT": price_sources.ROOT,
        "report_ROOT": build_html_report.ROOT,
        "backtest_PREDICTIONS_DIR": backtest.PREDICTIONS_DIR,
        "backtest_RESULTS_DIR": backtest.RESULTS_DIR,
        "daily_PREDICTIONS_DIR": daily.PREDICTIONS_DIR,
        "daily_RESULTS_DIR": daily.RESULTS_DIR,
        "daily_LEARNINGS_DIR": daily.LEARNINGS_DIR,
        "daily_REPORT_DIR": daily.REPORT_DIR,
        "daily_SIGNALS_DIR": daily.SIGNALS_DIR,
        "daily_MODEL_OUTPUT_DIR": daily.MODEL_OUTPUT_DIR,
        "daily_BIAS_REVIEW_DIR": daily.BIAS_REVIEW_DIR,
        "daily_PREDICTION_OUTPUT_DIR": daily.PREDICTION_OUTPUT_DIR,
        "price_PRICES_DIR": price_sources.PRICES_DIR,
        "prediction_LEARNINGS_DIR": prediction_engine.LEARNINGS_DIR,
        "prediction_PREDICTIONS_DIR": prediction_engine.PREDICTIONS_DIR,
        "report_REPORT_DIR": build_html_report.REPORT_DIR,
        "report_LATEST_DIR": build_html_report.LATEST_DIR,
        "report_DATA_DIR": build_html_report.DATA_DIR,
        "report_SNAPSHOT_DIR": build_html_report.SNAPSHOT_DIR,
        "calculate_hit_result": daily.calculate_hit_result,
    }

    daily.ROOT = root
    price_sources.ROOT = root
    build_html_report.ROOT = root

    backtest.PREDICTIONS_DIR = root / "backtest" / "predictions"
    backtest.RESULTS_DIR = root / "backtest" / "results"
    daily.PREDICTIONS_DIR = backtest.PREDICTIONS_DIR
    daily.RESULTS_DIR = backtest.RESULTS_DIR
    daily.LEARNINGS_DIR = root / "backtest" / "learnings"
    daily.REPORT_DIR = root / "reports"
    daily.SIGNALS_DIR = root / "signals"
    daily.MODEL_OUTPUT_DIR = root / "model_outputs"
    daily.BIAS_REVIEW_DIR = daily.MODEL_OUTPUT_DIR / "bias_review"
    daily.PREDICTION_OUTPUT_DIR = daily.MODEL_OUTPUT_DIR / "predictions"
    price_sources.PRICES_DIR = root / "prices"
    prediction_engine.LEARNINGS_DIR = root / "prediction_engine" / "learnings"
    prediction_engine.PREDICTIONS_DIR = root / "prediction_engine" / "predictions"

    build_html_report.REPORT_DIR = root / "reports"
    build_html_report.LATEST_DIR = build_html_report.REPORT_DIR / "latest"
    build_html_report.DATA_DIR = build_html_report.REPORT_DIR / "data"
    build_html_report.SNAPSHOT_DIR = build_html_report.DATA_DIR / "snapshots"
    return original


def _restore_paths(original: dict[str, Any]) -> None:
    backtest = daily.backtest_engine
    daily.ROOT = original["daily_ROOT"]
    price_sources.ROOT = original["price_ROOT"]
    build_html_report.ROOT = original["report_ROOT"]
    backtest.PREDICTIONS_DIR = original["backtest_PREDICTIONS_DIR"]
    backtest.RESULTS_DIR = original["backtest_RESULTS_DIR"]
    daily.PREDICTIONS_DIR = original["daily_PREDICTIONS_DIR"]
    daily.RESULTS_DIR = original["daily_RESULTS_DIR"]
    daily.LEARNINGS_DIR = original["daily_LEARNINGS_DIR"]
    daily.REPORT_DIR = original["daily_REPORT_DIR"]
    daily.SIGNALS_DIR = original["daily_SIGNALS_DIR"]
    daily.MODEL_OUTPUT_DIR = original["daily_MODEL_OUTPUT_DIR"]
    daily.BIAS_REVIEW_DIR = original["daily_BIAS_REVIEW_DIR"]
    daily.PREDICTION_OUTPUT_DIR = original["daily_PREDICTION_OUTPUT_DIR"]
    price_sources.PRICES_DIR = original["price_PRICES_DIR"]
    prediction_engine.LEARNINGS_DIR = original["prediction_LEARNINGS_DIR"]
    prediction_engine.PREDICTIONS_DIR = original["prediction_PREDICTIONS_DIR"]
    build_html_report.REPORT_DIR = original["report_REPORT_DIR"]
    build_html_report.LATEST_DIR = original["report_LATEST_DIR"]
    build_html_report.DATA_DIR = original["report_DATA_DIR"]
    build_html_report.SNAPSHOT_DIR = original["report_SNAPSHOT_DIR"]
    daily.calculate_hit_result = original["calculate_hit_result"]


def _check_iron_ore_cache_parse() -> None:
    price_sources.cache_price(
        "IRON_ORE",
        801.5,
        "CNY/ton",
        "2026-06-23",
        "smoke",
        force=True,
    )
    price_sources.cache_price(
        "AL",
        20700,
        "CNY/ton",
        "2026-06-23",
        "smoke",
        force=True,
    )
    metals = price_sources.list_cached_metals("2026-06-23")
    price_map = price_sources.build_price_map("2026-06-23")
    assert "IRON_ORE" in metals, metals
    assert "IRON" not in metals, metals
    assert "IRON_ORE" in price_map, price_map
    assert "IRON" not in price_map, price_map


def _check_verify_writes_result_and_learning() -> None:
    daily.record_prediction(
        "AL",
        100.0,
        "up",
        [0.01, 0.05],
        "medium",
        "smoke prediction",
        date_d="2026-06-22",
        target_date="2026-06-23",
        force=True,
    )
    price_sources.cache_price(
        "AL",
        90.0,
        "CNY/ton",
        "2026-06-23",
        "smoke-cache",
        force=True,
    )

    def miss_with_learning(
        prediction: daily.PredictionRecord,
        actual_price: float,
        actual_source: str = "",
    ) -> daily.ResultRecord:
        return daily.ResultRecord(
            metal=prediction.metal,
            prediction_date=prediction.date_d,
            actual_date=prediction.target_date,
            price_d=prediction.price_d,
            price_d_plus_1=actual_price,
            actual_change_pct=-0.1,
            predicted_direction=prediction.predicted_direction,
            predicted_range_pct=prediction.predicted_range_pct,
            actual_direction="down",
            hit_direction=False,
            hit_range=False,
            error_pct=-0.11,
            actual_price_source=actual_source,
            bias_reason="smoke miss reason",
            next_adjustment="when smoke trigger appears, narrow the bullish range",
        )

    daily.calculate_hit_result = miss_with_learning
    _, manifest = daily.run_verify_phase("2026-06-23")
    result_path = daily.backtest_engine.result_path("AL", "2026-06-22")
    learning_path = daily.learnings_path("AL")
    assert result_path.exists(), result_path
    assert learning_path.exists(), learning_path
    assert manifest["pending_verifications"] == 0, manifest
    assert manifest["learnings_written"] == [str(learning_path.relative_to(daily.ROOT))], manifest

    result_mtime = result_path.stat().st_mtime_ns
    learning_text = learning_path.read_text(encoding="utf-8")
    _, manifest = daily.run_verify_phase("2026-06-23")
    assert manifest["learnings_written"] == [], manifest
    assert result_path.stat().st_mtime_ns == result_mtime
    assert learning_path.read_text(encoding="utf-8") == learning_text

    price_sources.cache_price(
        "AL",
        88.0,
        "CNY/ton",
        "2026-06-23",
        "force-cache",
        force=True,
    )
    _, manifest = daily.run_verify_phase("2026-06-23", force=True)
    result = daily.load_result(result_path)
    assert result.price_d_plus_1 == 88.0, result
    assert result.actual_price_source == "force-cache", result
    assert manifest["learnings_written"] == [str(learning_path.relative_to(daily.ROOT))], manifest


def _check_external_bias_review_is_applied() -> None:
    daily.record_prediction(
        "CO",
        100.0,
        "down",
        [-0.05, -0.01],
        "medium",
        "external bias smoke",
        date_d="2026-06-24",
        target_date="2026-06-25",
        force=True,
    )
    price_sources.cache_price(
        "CO",
        110.0,
        "CNY/ton",
        "2026-06-25",
        "smoke-cache",
        force=True,
    )
    out = daily.bias_review_output_path("CO", "2026-06-24")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "bias_reason": "external model named the wrong catalyst",
                "next_adjustment": "when this catalyst appears, set direction to flat",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _, manifest = daily.run_verify_phase("2026-06-25")
    result = daily.load_existing_result("CO", "2026-06-24")
    learning_path = daily.learnings_path("CO")
    assert result is not None
    assert result.bias_reason == "external model named the wrong catalyst", result
    assert result.next_adjustment == "when this catalyst appears, set direction to flat", result
    assert learning_path.exists(), learning_path
    assert str(learning_path.relative_to(daily.ROOT)) in manifest["learnings_written"], manifest


def _check_external_prediction_is_consumed() -> None:
    price_sources.cache_price(
        "SN",
        250000.0,
        "CNY/ton",
        "2026-06-26",
        "smoke-cache",
        force=True,
    )
    out = daily.prediction_output_path("SN", "2026-06-26")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "predicted_direction": "up",
                "predicted_range_pct": [0.01, 0.03],
                "confidence": "medium",
                "rationale": "external prediction smoke",
                "used_learnings": ["external rule"],
                "signals": [
                    {
                        "source": "news",
                        "event": "smoke",
                        "direction": "up",
                        "strength": "medium",
                        "timing": "D-day",
                        "brief": "smoke signal",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _, manifest = daily.run_predict_phase("2026-06-26", metals=["SN"], force=True)
    pred_path = daily.prediction_path("SN", "2026-06-26")
    pred = daily.load_prediction(pred_path)
    assert pred.predicted_direction == "up", pred
    assert pred.used_learnings == ["external rule"], pred
    assert pred.signals and pred.signals[0]["brief"] == "smoke signal", pred
    assert str(pred_path.relative_to(daily.ROOT)) in manifest["predictions_written"], manifest


def _check_signals_fallback_prediction() -> None:
    price_sources.cache_price(
        "PB",
        18000.0,
        "CNY/ton",
        "2026-06-27",
        "smoke-cache",
        force=True,
    )
    out = daily.signal_output_path("PB", "2026-06-27")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "signals": [
                    {
                        "source": "supply_chain",
                        "event": "mine halt",
                        "direction": "up",
                        "strength": "high",
                        "timing": "D-day",
                        "brief": "停产导致供应收缩",
                    },
                    {
                        "source": "international",
                        "event": "premium",
                        "direction": "up",
                        "strength": "medium",
                        "timing": "ongoing",
                        "brief": "LME现货升水",
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _, manifest = daily.run_predict_phase("2026-06-27", metals=["PB"], force=True)
    pred_path = daily.prediction_path("PB", "2026-06-27")
    pred = daily.load_prediction(pred_path)
    assert pred.signals and len(pred.signals) == 2, pred
    assert pred.predicted_direction in {"up", "flat", "volatile", "down"}, pred
    assert str(pred_path.relative_to(daily.ROOT)) in manifest["predictions_written"], manifest


def _check_first_day_report_generation() -> None:
    pred = daily.PredictionRecord(
        metal="CU",
        date_d="2026-06-23",
        target_date="2026-06-24",
        price_d=70000.0,
        price_unit="CNY/ton",
        price_source="smoke",
        predicted_direction="flat",
        predicted_range_pct=[-0.005, 0.005],
        confidence="medium",
        rationale="baseline",
    )
    review = daily.YesterdayReview(
        date="2026-06-23",
        total_predictions=0,
        verified_count=0,
        pending_count=0,
        summary_text="2026-06-23: No predictions to review.",
    )
    report = daily.assemble_daily_report_json(
        "2026-06-23",
        [pred],
        [],
        {},
        review,
    )
    expected_headline = "昨天没有登记的一日预测，今天先建立基准预测记录"
    assert report["yesterday_review"]["headline"] == expected_headline, report
    assert report["yesterday_review"]["backtests"] == [], report
    assert report["yesterday_review"]["learnings_written"] == [], report
    errors = build_html_report._validate_report(report, "daily_briefing")
    assert errors == [], errors
    report_path, index_path, json_path = daily.generate_and_publish_report(report)
    assert report_path.exists(), report_path
    assert index_path.exists(), index_path
    assert json_path.exists(), json_path


def _check_yesterday_review_first_block() -> None:
    report = {
        "report_id": "order_smoke",
        "report_type": "daily_briefing",
        "date": "2026-06-23",
        "title": "Order smoke",
        "summary": "Order smoke summary",
        "metals": [
            {
                "code": "AL",
                "name": "Aluminum",
                "spot_price": 20700,
                "unit": "CNY/ton",
                "direction": "flat",
                "range_pct": [-0.005, 0.005],
                "confidence": "medium",
                "rationale": "baseline",
            }
        ],
        "yesterday_review": {
            "headline": "headline",
            "summary": "summary",
            "backtests": [],
            "learnings_written": [],
        },
    }
    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    rendered = (
        build_html_report.REPORT_TEMPLATE.replace("__TITLE__", html.escape(report["title"]))
        .replace("__TYPE_LABEL__", html.escape(build_html_report.REPORT_TYPES["daily_briefing"]["label"]))
        .replace("__DATE__", html.escape(report["date"]))
        .replace("__REPORT_ID__", html.escape(report["report_id"]))
        .replace("__SUMMARY__", html.escape(report["summary"]))
        .replace("__INDEX_HREF__", "../index.html")
        .replace("__REPORT_JSON__", build_html_report._escape_script_json(report_json))
        .replace("__REPORT_TYPES_JSON__", json.dumps(build_html_report.REPORT_TYPES, ensure_ascii=False))
    )
    assert "const yesterdayReviewPanel = () =>" in rendered
    assert "yr.headline" in rendered
    assert "yr.summary" in rendered
    assert "yr.backtests" in rendered
    assert "yr.learnings_written" in rendered
    assert rendered.index("let body = yesterdayReviewPanel();") < rendered.index('body += panel("Report Type"')


if __name__ == "__main__":
    raise SystemExit(main())
