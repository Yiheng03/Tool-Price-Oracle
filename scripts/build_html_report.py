#!/usr/bin/env python3
"""Build typed standalone HTML reports and a static report index from report JSON."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _escape_script_json(json_str: str) -> str:
    """Escape only </script> sequences so JSON is safe inside a <script> tag.

    html.escape() must NOT be used here — in script raw text, &quot; is not
    decoded back to \" by the browser, so JSON.parse() would fail.
    """
    return json_str.replace("</", "<\\/")

REPORT_DIR = ROOT / ".workbuddy" / "memory" / "reports"
LATEST_DIR = REPORT_DIR / "latest"
DATA_DIR = REPORT_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

REPORT_TYPES = {
    "tool_price": {
        "label": "Tool price and purchasing",
        "snapshot": "forecast",
        "description": "Full cutting-tool raw material, cost impact, and purchasing action report.",
    },
    "single_metal": {
        "label": "Single metal outlook",
        "snapshot": "forecast",
        "description": "One target metal with spot price, D+1 direction, range, and rationale.",
    },
    "daily_briefing": {
        "label": "Daily metal briefing",
        "snapshot": "forecast",
        "description": "Daily multi-metal market briefing with news, supply chain, and next-day view.",
    },
    "weekly_briefing": {
        "label": "Weekly metal briefing",
        "snapshot": "forecast",
        "description": "Weekly summary of registered one-day predictions, results, and learnings.",
    },
    "backtest": {
        "label": "Prediction backtest",
        "snapshot": "backtest",
        "description": "D+1 verification report for prior predictions and bias learnings.",
    },
}


def _is_number(value: Any) -> bool:
    """True if value is a JSON Schema number: int or float, but never bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_report(report: dict[str, Any], rtype: str) -> list[str]:
    """Validate a report dict against the schema. Returns a list of error messages."""
    errors = []
    # top-level required string fields
    for key in ("report_id", "date", "title", "summary"):
        if not isinstance(report.get(key), str) or not str(report.get(key)).strip():
            errors.append("top-level %r must be a non-empty string" % key)
    # date format
    date_val = str(report.get("date", ""))
    if date_val and not re.match(r"^\d{4}-\d{2}-\d{2}$", date_val):
        errors.append("date must be YYYY-MM-DD format, got %r" % date_val)
    # metals array
    metals = report.get("metals")
    if not isinstance(metals, list) or len(metals) == 0:
        errors.append("metals must be a non-empty array")
    else:
        for i, metal in enumerate(metals):
            if not isinstance(metal, dict):
                errors.append("metals[%d] must be an object, got %s" % (i, type(metal).__name__))
                continue
            prefix = "metals[%d]" % i
            for fld in ("code", "name", "unit", "confidence", "rationale"):
                if not isinstance(metal.get(fld), str) or not str(metal.get(fld)).strip():
                    errors.append("%s.%s must be a non-empty string" % (prefix, fld))
            direction = metal.get("direction")
            if not isinstance(direction, str) or direction not in {"up", "down", "flat", "volatile"}:
                errors.append("%s.direction must be one of: up, down, flat, volatile" % prefix)
            if not _is_number(metal.get("spot_price")):
                errors.append("%s.spot_price must be a number" % prefix)
            rng = metal.get("range_pct")
            if not isinstance(rng, list) or len(rng) != 2 or not all(_is_number(v) for v in rng):
                errors.append("%s.range_pct must be [number, number]" % prefix)
    # tool_price conditional requirements
    if rtype == "tool_price":
        tci = report.get("tool_cost_impact")
        if not isinstance(tci, dict):
            errors.append("tool_cost_impact is required for tool_price reports and must be an object")
        else:
            for fld in ("tool_spec", "main_driver"):
                if not isinstance(tci.get(fld), str) or not str(tci.get(fld)).strip():
                    errors.append("tool_cost_impact.%s must be a non-empty string" % fld)
            for fld in ("base_cost", "next_day_low", "next_day_high"):
                if not _is_number(tci.get(fld)):
                    errors.append("tool_cost_impact.%s must be a number" % fld)
    # recommendation object
    rec = report.get("recommendation")
    if rec is not None:
        if not isinstance(rec, dict):
            errors.append("recommendation must be an object")
        else:
            for fld in ("action", "reason", "risk_level"):
                if not isinstance(rec.get(fld), str) or not str(rec.get(fld)).strip():
                    errors.append("recommendation.%s must be a non-empty string" % fld)
    # sections array
    sections = report.get("sections")
    if sections is not None:
        if not isinstance(sections, list):
            errors.append("sections must be an array")
        else:
            for i, sec in enumerate(sections):
                if not isinstance(sec, dict):
                    errors.append("sections[%d] must be an object" % i)
                    continue
                if not isinstance(sec.get("title"), str) or not str(sec.get("title")).strip():
                    errors.append("sections[%d].title must be a non-empty string" % i)
    # backtests array
    backtests = report.get("backtests")
    if backtests is not None:
        if not isinstance(backtests, list):
            errors.append("backtests must be an array")
        else:
            for i, bt in enumerate(backtests):
                if not isinstance(bt, dict):
                    errors.append("backtests[%d] must be an object" % i)
    # yesterday_review — required by schema for tool_price and daily_briefing
    yr = report.get("yesterday_review")
    if rtype in ("tool_price", "daily_briefing"):
        if not isinstance(yr, dict):
            errors.append("yesterday_review is required for %s reports and must be an object" % rtype)
        else:
            for fld in ("headline", "summary"):
                if not isinstance(yr.get(fld), str) or not str(yr.get(fld)).strip():
                    errors.append("yesterday_review.%s must be a non-empty string" % fld)
            yr_backtests = yr.get("backtests")
            if not isinstance(yr_backtests, list):
                errors.append("yesterday_review.backtests must be an array")
            yr_learnings = yr.get("learnings_written")
            if not isinstance(yr_learnings, list):
                errors.append("yesterday_review.learnings_written must be an array")
    return errors


REPORT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      --primary: #1a365d;
      --accent: #2b6cb0;
      --up: #e53e3e;
      --down: #38a169;
      --flat: #718096;
      --warn: #b7791f;
      --bg: #f7fafc;
      --card: #ffffff;
      --border: #e2e8f0;
      --text: #2d3748;
      --muted: #718096;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 20px;
    }
    .container { max-width: 1100px; margin: 0 auto; }
    .header {
      background: linear-gradient(135deg, var(--primary), var(--accent));
      color: #fff;
      padding: 24px 32px;
      border-radius: 12px;
      margin-bottom: 24px;
      box-shadow: 0 8px 20px rgba(26, 54, 93, 0.18);
    }
    .header h1 { font-size: 24px; line-height: 1.25; margin-bottom: 6px; letter-spacing: 0; }
    .header .sub { opacity: 0.86; font-size: 14px; line-height: 1.6; }
    .header .badge {
      display: inline-block;
      background: rgba(255,255,255,0.18);
      border: 1px solid rgba(255,255,255,0.22);
      padding: 4px 11px;
      border-radius: 12px;
      font-size: 12px;
      margin-top: 10px;
      line-height: 1.5;
    }
    .header .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
    .header a, .header button {
      border: 1px solid rgba(255,255,255,0.36);
      background: rgba(255,255,255,0.16);
      color: #fff;
      border-radius: 8px;
      padding: 7px 10px;
      font: inherit;
      font-size: 12px;
      cursor: pointer;
      text-decoration: none;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }
    .summary-item {
      text-align: center;
      padding: 16px;
      border-radius: 8px;
      background: var(--card);
      border: 1px solid var(--border);
      box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }
    .summary-item .label { font-size: 12px; color: var(--muted); min-height: 18px; }
    .summary-item .price { font-size: 22px; font-weight: 700; margin: 4px 0; color: #1a202c; }
    .summary-item .change { font-size: 13px; font-weight: 650; }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 20px 24px;
      margin-bottom: 20px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }
    .card h2 {
      font-size: 17px;
      color: var(--primary);
      margin-bottom: 14px;
      padding-bottom: 8px;
      border-bottom: 2px solid var(--accent);
      letter-spacing: 0;
    }
    .card h3 { font-size: 14px; color: var(--accent); margin: 12px 0 8px; letter-spacing: 0; }
    .card p { font-size: 13px; line-height: 1.8; margin-bottom: 8px; }
    .muted { color: var(--muted); }
    .table-wrap { width: 100%; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th {
      background: #edf2f7;
      text-align: left;
      padding: 8px 10px;
      font-weight: 600;
      font-size: 12px;
      color: #4a5568;
      white-space: nowrap;
    }
    td {
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
      line-height: 1.55;
    }
    tr:hover td { background: #f7fafc; }
    tr:last-child td { border-bottom: none; }
    ul { padding-left: 18px; font-size: 13px; line-height: 1.8; }
    li { margin: 4px 0; }
    .tag-up { color: var(--up); font-weight: 650; }
    .tag-down { color: var(--down); font-weight: 650; }
    .tag-flat { color: var(--flat); font-weight: 650; }
    .tag-volatile { color: var(--warn); font-weight: 650; }
    .tag-hit, .tag-miss {
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 650;
    }
    .tag-hit { background: #c6f6d5; color: #22543d; }
    .tag-miss { background: #fed7d7; color: #9b2c2c; }
    .bias-box {
      background: #fffbeb;
      border: 1px solid #f6e05e;
      border-radius: 6px;
      padding: 12px;
      margin-top: 10px;
      font-size: 13px;
      line-height: 1.75;
    }
    .bias-box strong { color: #744210; }
    .strategy-box {
      background: #ebf8ff;
      border: 1px solid #bee3f8;
      border-radius: 6px;
      padding: 14px;
      font-size: 13px;
      line-height: 1.8;
    }
    .strategy-box strong { color: #2a4365; }
    .metric-table td:first-child { width: 180px; color: #4a5568; }
    .footer {
      text-align: center;
      background: #edf2f7;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.8;
    }
    pre {
      max-height: 360px;
      overflow: auto;
      background: #111827;
      color: #e5e7eb;
      border-radius: 6px;
      padding: 14px;
      font-size: 12px;
      line-height: 1.5;
    }
    @media (max-width: 768px) {
      body { padding: 12px; }
      .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .header { padding: 16px 20px; }
      .header h1 { font-size: 18px; }
      .card { padding: 16px; }
      table { font-size: 12px; }
      td, th { padding: 6px 8px; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>__TITLE__</h1>
      <div class="sub">__DATE__ · __TYPE_LABEL__ · __REPORT_ID__</div>
      <div class="badge">今日核心：__SUMMARY__</div>
      <div class="actions">
        <a href="__INDEX_HREF__">报告中心</a>
        <button id="download-json">下载 JSON</button>
        <button id="toggle-json">查看 JSON</button>
      </div>
    </div>
    <div id="report-root"></div>
  </div>
  <script type="application/json" id="report-data">__REPORT_JSON__</script>
  <script>
    const report = JSON.parse(document.getElementById("report-data").textContent);
    const root = document.getElementById("report-root");
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
    const directionText = { up: "看涨", down: "看跌", flat: "横盘", volatile: "震荡" };
    const confidenceText = { high: "高", medium: "中", low: "低" };
    const classForDirection = (value) => {
      if (value === "up") return "tag-up";
      if (value === "down") return "tag-down";
      if (value === "volatile") return "tag-volatile";
      return "tag-flat";
    };
    const fmtPct = (range) => {
      if (!Array.isArray(range) || range.length < 2) return "";
      const low = Number(range[0]) * 100;
      const high = Number(range[1]) * 100;
      if (Math.abs(low + high) < 0.001) return `±${Math.abs(high).toFixed(2)}%`;
      return `${low.toFixed(2)}% ~ ${high.toFixed(2)}%`;
    };
    const fmtMoney = (value, currency = "") => {
      if (value === undefined || value === null || value === "") return "";
      const number = Number(value);
      const rendered = Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: 2 }) : value;
      return `${currency || ""} ${rendered}`.trim();
    };
    const card = (title, body, extraClass = "") => `
      <div class="card ${extraClass}">
        <h2>${escapeHtml(title)}</h2>
        ${body}
      </div>
    `;
    const rows = (items, columns) => items.map((item) => `
      <tr>${columns.map(([key]) => `<td>${escapeHtml(item?.[key] ?? "")}</td>`).join("")}</tr>
    `).join("");
    const table = (items, columns) => {
      if (!Array.isArray(items) || items.length === 0) return '<p class="muted">暂无数据。</p>';
      return `
        <div class="table-wrap"><table>
          <thead><tr>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead>
          <tbody>${rows(items, columns)}</tbody>
        </table></div>
      `;
    };
    const summaryGrid = () => {
      const metals = Array.isArray(report.metals) ? report.metals.slice(0, 4) : [];
      if (metals.length === 0) return "";
      return `<div class="summary-grid">${metals.map((metal) => `
        <div class="summary-item">
          <div class="label">${escapeHtml(metal.name || metal.code)}</div>
          <div class="price">${escapeHtml(fmtMoney(metal.spot_price, metal.currency))}</div>
          <div class="change ${classForDirection(metal.direction)}">${escapeHtml(directionText[metal.direction] || metal.direction || "横盘")} ${escapeHtml(fmtPct(metal.range_pct))}</div>
        </div>
      `).join("")}</div>`;
    };
    const metalTable = () => {
      const metals = report.metals || [];
      if (!Array.isArray(metals) || metals.length === 0) return '<p class="muted">暂无数据。</p>';
      return `
        <div class="table-wrap"><table>
          <thead>
            <tr>
              <th>金属</th>
              <th>今日现货</th>
              <th>单位</th>
              <th>D+1 方向</th>
              <th>幅度区间</th>
              <th>置信度</th>
              <th>核心理由</th>
            </tr>
          </thead>
          <tbody>
            ${metals.map((metal) => `
              <tr>
                <td><strong>${escapeHtml(metal.name || metal.code)}</strong><br><span class="muted">${escapeHtml(metal.code)}</span></td>
                <td>${escapeHtml(fmtMoney(metal.spot_price, metal.currency))}</td>
                <td>${escapeHtml(metal.unit)}</td>
                <td><span class="${classForDirection(metal.direction)}">${escapeHtml(directionText[metal.direction] || metal.direction || "")}</span></td>
                <td>${escapeHtml(fmtPct(metal.range_pct))}</td>
                <td>${escapeHtml(confidenceText[metal.confidence] || metal.confidence || "")}</td>
                <td>${escapeHtml(metal.rationale)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table></div>
      `;
    };
    const recommendationPanel = () => {
      if (!report.recommendation) return "";
      return card("采购策略", `
        <div class="strategy-box">
          <p><strong>综合建议：</strong>${escapeHtml(report.recommendation.action)}</p>
          <p><strong>风险等级：</strong>${escapeHtml(report.recommendation.risk_level)}</p>
          <p>${escapeHtml(report.recommendation.reason)}</p>
        </div>
      `);
    };
    const costPanel = () => {
      const impact = report.tool_cost_impact;
      if (!impact) return "";
      return card("刀具成本影响", `
        <div class="table-wrap"><table class="metric-table"><tbody>
          <tr><td>材质/场景</td><td>${escapeHtml(impact.tool_spec)}</td></tr>
          <tr><td>当前成本基准</td><td>${escapeHtml(fmtMoney(impact.base_cost, impact.currency))}</td></tr>
          <tr><td>次日成本风险区间</td><td><strong>${escapeHtml(fmtMoney(impact.next_day_low, impact.currency))} ~ ${escapeHtml(fmtMoney(impact.next_day_high, impact.currency))}</strong></td></tr>
          <tr><td>主要驱动</td><td>${escapeHtml(impact.main_driver)}</td></tr>
        </tbody></table></div>
      `);
    };
    const backtestPanel = () => {
      const yesterday = report.json_payload?.yesterday_backtest;
      const items = report.backtests || report.json_payload?.backtests || (yesterday ? [yesterday] : []);
      if (!Array.isArray(items) || items.length === 0) return "";
      return card("预测复盘", table(items, [
        ["metal", "金属"],
        ["prediction_date", "预测日期"],
        ["actual_date", "实际日期"],
        ["predicted_direction", "预测方向"],
        ["actual_direction", "实际方向"],
        ["hit_direction", "方向命中"],
        ["hit_range", "区间命中"],
        ["bias_reason", "偏差原因"],
        ["next_adjustment", "调整规则"],
      ]));
    };
    const yesterdayReviewPanel = () => {
      const yr = report.yesterday_review;
      if (!yr || typeof yr !== "object") return "";
      const backtests = Array.isArray(yr.backtests) ? yr.backtests : [];
      const total = backtests.length;
      const hits = backtests.filter((bt) => bt.hit_direction).length;
      const hitRate = total > 0 ? ((hits / total) * 100).toFixed(1) : "0.0";
      let body = `
        <div class="bias-box">
          <strong>${escapeHtml(yr.headline || "复盘摘要")}</strong><br>
          方向命中率：<strong>${hitRate}%</strong>（${hits}/${total}）
        </div>
      `;
      if (yr.summary) {
        body += `<p>${escapeHtml(yr.summary)}</p>`;
      }
      if (Array.isArray(yr.learnings_written) && yr.learnings_written.length > 0) {
        body += `<h3>偏差学习记录</h3><ul>${yr.learnings_written.map((l) => `<li>${escapeHtml(typeof l === "string" ? l : JSON.stringify(l))}</li>`).join("")}</ul>`;
      }
      if (backtests.length > 0) {
        body += `<h3>逐项复核</h3>
        <div class="table-wrap"><table>
          <thead><tr>
            <th>金属</th><th>预测方向</th><th>实际方向</th><th>方向</th><th>区间</th><th>偏差原因</th><th>调整规则</th>
          </tr></thead>
          <tbody>
            ${backtests.map((bt) => `
              <tr>
                <td><strong>${escapeHtml(bt.metal || "")}</strong></td>
                <td><span class="${classForDirection(bt.predicted_direction)}">${escapeHtml(directionText[bt.predicted_direction] || bt.predicted_direction || "")}</span></td>
                <td><span class="${classForDirection(bt.actual_direction)}">${escapeHtml(directionText[bt.actual_direction] || bt.actual_direction || "")}</span></td>
                <td><span class="${bt.hit_direction ? "tag-hit" : "tag-miss"}">${bt.hit_direction ? "命中" : "未中"}</span></td>
                <td><span class="${bt.hit_range ? "tag-hit" : "tag-miss"}">${bt.hit_range ? "命中" : "未中"}</span></td>
                <td>${escapeHtml(bt.bias_reason || "")}</td>
                <td>${escapeHtml(bt.next_adjustment || "")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table></div>`;
      }
      return card("昨日预测复盘", body);
    };
    const sectionsPanel = () => {
      if (!Array.isArray(report.sections) || report.sections.length === 0) return "";
      return report.sections.map((section) => {
        let body = "";
        if (section.body) body += `<p>${escapeHtml(section.body)}</p>`;
        if (Array.isArray(section.items)) {
          body += `<ul>${section.items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
        }
        if (Array.isArray(section.rows) && Array.isArray(section.columns)) {
          body += table(section.rows, section.columns.map((col) => [col.key, col.label || col.key]));
        }
        return card(section.title || "明细", body || '<p class="muted">暂无明细。</p>');
      }).join("");
    };
    const type = report.report_type || "tool_price";
    let body = summaryGrid();
    if (type === "tool_price") {
      body += card("当日金属现货与明日判断", metalTable());
      body += sectionsPanel();
      body += yesterdayReviewPanel();
      body += costPanel();
      body += recommendationPanel();
    } else if (type === "single_metal") {
      body += card("单金属明日判断", metalTable());
      body += sectionsPanel();
      body += recommendationPanel();
    } else if (type === "daily_briefing" || type === "weekly_briefing") {
      body += card("当日金属现货与明日判断", metalTable());
      body += sectionsPanel();
      body += yesterdayReviewPanel();
      body += recommendationPanel();
    } else if (type === "backtest") {
      body += backtestPanel();
      body += sectionsPanel();
      body += recommendationPanel();
    } else {
      body += card("当日金属现货与明日判断", metalTable());
      body += sectionsPanel();
      body += recommendationPanel();
    }
    body += `<div id="json-panel" hidden class="card"><h2>原始 JSON</h2><pre id="json-view"></pre></div>`;
    body += `<div class="card footer">刀具行情参谋团 · 自动化报告 · 数据来自结构化 JSON<br>发布时间：${escapeHtml(new Date().toLocaleString())}</div>`;
    root.innerHTML = body;
    document.getElementById("json-view").textContent = JSON.stringify(report, null, 2);
    document.getElementById("toggle-json").addEventListener("click", () => {
      const panel = document.getElementById("json-panel");
      panel.hidden = !panel.hidden;
      document.getElementById("toggle-json").textContent = panel.hidden ? "查看 JSON" : "隐藏 JSON";
    });
    document.getElementById("download-json").addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${report.report_id}.json`;
      a.click();
      URL.revokeObjectURL(url);
    });
  </script>
</body>
</html>
"""


INDEX_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tool Price Report Center</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #16202a;
      --muted: #657181;
      --line: #dfe5ec;
      --accent: #0f766e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 28px min(5vw, 56px) 20px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }
    main { padding: 24px min(5vw, 56px) 44px; }
    h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }
    p { color: var(--muted); line-height: 1.6; }
    input {
      width: min(680px, 100%);
      margin-top: 14px;
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font: inherit;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 12px 10px;
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }
    th { color: var(--muted); font-weight: 650; }
    tr:last-child td { border-bottom: 0; }
    a {
      color: var(--accent);
      font-weight: 650;
      text-decoration: none;
    }
    .muted { color: var(--muted); }
    .empty {
      background: var(--panel);
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 24px;
      color: var(--muted);
    }
    @media (max-width: 760px) {
      table, thead, tbody, tr, th, td { display: block; }
      thead { display: none; }
      tr { border-bottom: 1px solid var(--line); }
      td { border-bottom: 0; padding: 8px 12px; }
      td::before {
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 12px;
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>Tool Price Report Center</h1>
    <p>Static index generated from typed JSON reports. Open an HTML report, inspect the embedded JSON, or download the source JSON from each report page.</p>
    <input id="filter" type="search" placeholder="Filter by type, title, date, metal, action, or risk level">
  </header>
  <main>
    <table id="reports">
      <thead>
        <tr>
          <th>Report Date</th>
          <th>Updated</th>
          <th>Type</th>
          <th>Title</th>
          <th>Metals</th>
          <th>Recommendation</th>
          <th>Risk</th>
          <th>Open</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
    <div class="empty" id="empty" hidden>No reports match the current filter.</div>
  </main>
  <script type="application/json" id="report-index">__REPORTS_JSON__</script>
  <script>
    const reports = JSON.parse(document.getElementById("report-index").textContent);
    const tbody = document.querySelector("#reports tbody");
    const empty = document.getElementById("empty");
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
    function render(filter = "") {
      const needle = filter.trim().toLowerCase();
      const filtered = reports.filter((item) => JSON.stringify(item).toLowerCase().includes(needle));
      tbody.innerHTML = filtered.map((item) => `
        <tr>
          <td data-label="Report Date">${escapeHtml(item.date)}</td>
          <td data-label="Updated">${escapeHtml(item.updated_at || "")}</td>
          <td data-label="Type">${escapeHtml(item.report_type_label)}</td>
          <td data-label="Title"><strong>${escapeHtml(item.title)}</strong><br><span class="muted">${escapeHtml(item.report_id)}</span></td>
          <td data-label="Metals">${escapeHtml(item.metals.join(", "))}</td>
          <td data-label="Recommendation">${escapeHtml(item.action)}</td>
          <td data-label="Risk">${escapeHtml(item.risk_level)}</td>
          <td data-label="Open"><a href="${escapeHtml(item.html_file)}">Open report</a><br><a class="muted" href="${escapeHtml(item.json_file)}">Source JSON</a></td>
        </tr>
      `).join("");
      empty.hidden = filtered.length > 0;
      document.getElementById("reports").hidden = filtered.length === 0;
    }
    document.getElementById("filter").addEventListener("input", (event) => render(event.target.value));
    render();
  </script>
</body>
</html>
"""


def report_type(report: dict[str, Any]) -> str:
    value = str(report.get("report_type") or "").strip()
    if value in REPORT_TYPES:
        return value
    valid = ", ".join(sorted(REPORT_TYPES))
    if not value:
        raise ValueError(
            f"report is missing required key: report_type. "
            f"Valid values: {valid}"
        )
    raise ValueError(
        f"unknown report_type {value!r}. Valid values: {valid}"
    )


def load_report(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    missing = [
        key
        for key in ("report_id", "date", "title", "summary", "metals")
        if key not in report
    ]
    if missing:
        raise ValueError(f"{path} is missing required keys: {', '.join(missing)}")
    rtype = report_type(report)
    report["report_type"] = rtype

    validation_errors = _validate_report(report, rtype)
    if validation_errors:
        raise ValueError(
            f"{path} failed schema validation:\n  " + "\n  ".join(validation_errors)
        )

    payload = report.get("json_payload", {})
    payload_backtests = payload.get("backtests") if isinstance(payload, dict) else None
    if rtype == "backtest" and not report.get("backtests"):
        if not isinstance(payload_backtests, list) or len(payload_backtests) == 0:
            if not is_backtest_report(report):
                raise ValueError(
                    f"{path} is a backtest report but has no backtests "
                    f"and json_payload.backtests is not a non-empty list"
                )
    return report


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return slug or "report"


def topic_slug(report: dict[str, Any]) -> str:
    rtype = report_type(report)
    metals = [str(metal.get("code", "")).strip().upper() for metal in report.get("metals", [])]
    metals = [metal for metal in metals if metal]
    if metals:
        return slugify(f"{rtype}_{'_'.join(metals)}")
    return slugify(f"{rtype}_{str(report['report_id']).split('_')[0]}")


def is_backtest_report(report: dict[str, Any]) -> bool:
    payload = report.get("json_payload", {})
    backtest = payload.get("yesterday_backtest") if isinstance(payload, dict) else None
    return isinstance(backtest, dict) and backtest.get("status") == "verified"


def snapshot_kind(report: dict[str, Any]) -> str:
    return REPORT_TYPES[report_type(report)]["snapshot"]


def snapshot_date(report: dict[str, Any], kind: str) -> str:
    if kind == "backtest":
        for key in ("actual_date", "updated_date", "updated_at"):
            value = report.get(key)
            if isinstance(value, str) and value:
                return value[:10]
        return datetime.now().date().isoformat()
    return str(report["date"])[:10]


def with_storage_metadata(report: dict[str, Any], topic: str, kind: str, snapshot_name: str) -> dict[str, Any]:
    enriched = dict(report)
    generated_at = datetime.now().replace(microsecond=0).isoformat()
    enriched["report_storage"] = {
        "topic": topic,
        "report_type": report_type(report),
        "report_type_label": REPORT_TYPES[report_type(report)]["label"],
        "snapshot_kind": kind,
        "snapshot_file": f"snapshots/{snapshot_name}",
        "latest_json": f"{topic}.latest.json",
        "latest_html": f"../latest/{topic}.html",
        "updated_at": generated_at,
    }
    return enriched


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def persist_report_data(report: dict[str, Any]) -> tuple[dict[str, Any], Path, Path, str]:
    topic = topic_slug(report)
    kind = snapshot_kind(report)
    snap_date = snapshot_date(report, kind)
    report_id_slug = slugify(report["report_id"])
    snapshot_name = f"{topic}_{snap_date}_{report_id_slug}_{kind}.json"
    enriched = with_storage_metadata(report, topic, kind, snapshot_name)
    latest_json_path = DATA_DIR / f"{topic}.latest.json"
    snapshot_path = SNAPSHOT_DIR / snapshot_name
    write_json(latest_json_path, enriched)
    write_json(snapshot_path, enriched)
    return enriched, latest_json_path, snapshot_path, topic


def build_report(json_path: Path) -> tuple[Path, Path, Path]:
    report = load_report(json_path)
    report, latest_json_path, snapshot_path, topic = persist_report_data(report)
    rtype = report_type(report)
    report_id = slugify(report["report_id"])
    output_path = LATEST_DIR / f"{topic}.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    rendered = (
        REPORT_TEMPLATE.replace("__TITLE__", html.escape(report["title"]))
        .replace("__TYPE_LABEL__", html.escape(REPORT_TYPES[rtype]["label"]))
        .replace("__DATE__", html.escape(report["date"]))
        .replace("__REPORT_ID__", html.escape(report_id))
        .replace("__SUMMARY__", html.escape(report["summary"]))
        .replace("__INDEX_HREF__", "../index.html")
        .replace("__REPORT_JSON__", _escape_script_json(report_json))
    )
    output_path.write_text(rendered, encoding="utf-8")
    return output_path, latest_json_path, snapshot_path


def build_index() -> Path:
    reports = []
    for json_path in sorted(DATA_DIR.glob("*.latest.json")):
        report = load_report(json_path)
        report_id = slugify(report["report_id"])
        rtype = report_type(report)
        topic = report.get("report_storage", {}).get("topic") or topic_slug(report)
        html_path = LATEST_DIR / f"{topic}.html"
        if not html_path.exists():
            continue
        storage = report.get("report_storage", {})
        recommendation = report.get("recommendation", {})
        reports.append(
            {
                "report_id": report_id,
                "report_type": rtype,
                "report_type_label": REPORT_TYPES[rtype]["label"],
                "date": report["date"],
                "updated_at": str(storage.get("updated_at", ""))[:19],
                "title": report["title"],
                "metals": [metal["code"] for metal in report.get("metals", [])],
                "action": recommendation.get("action", ""),
                "risk_level": recommendation.get("risk_level", ""),
                "json_file": f"data/{json_path.name}",
                "html_file": f"latest/{html_path.name}",
            }
        )
    reports.sort(key=lambda item: (item["updated_at"], item["date"], item["report_id"]), reverse=True)
    output_path = REPORT_DIR / "index.html"
    rendered = INDEX_TEMPLATE.replace(
        "__REPORTS_JSON__",
        _escape_script_json(json.dumps(reports, ensure_ascii=False, indent=2)),
    )
    output_path.write_text(rendered, encoding="utf-8")
    return output_path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path_href(from_file: Path, target: Path) -> str:
    base = from_file.resolve().parent
    target = target.resolve()
    try:
        rel = target.relative_to(base)
        return rel.as_posix()
    except ValueError:
        try:
            rel = target.relative_to(base.parent)
            return Path("..").joinpath(rel).as_posix()
        except ValueError:
            return target.as_uri()


def _workbuddy_report_metadata(report_path: Path) -> dict[str, str]:
    title = report_path.stem
    date = report_path.parent.name
    rtype = ""
    json_path = report_path.with_suffix(".json")
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            title = str(data.get("title") or title)
            date = str(data.get("date") or date)
            rtype = str(data.get("report_type") or "")
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "title": title,
        "date": date,
        "type": rtype,
        "dir": report_path.parent.name,
        "updated": datetime.fromtimestamp(report_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def _workbuddy_index_html(index_dir: Path, report_paths: list[Path]) -> str:
    cards = []
    for report_path in report_paths:
        meta = _workbuddy_report_metadata(report_path)
        href = _path_href(index_dir / "index.html", report_path)
        cards.append(
            '<article class="card">'
            f'<a class="title" href="{html.escape(href)}">{html.escape(meta["title"])}</a>'
            f'<div class="meta">Report date: {html.escape(meta["date"])}'
            f' · Type: {html.escape(meta["type"])}'
            f' · Folder: {html.escape(meta["dir"])}'
            f' · Updated: {html.escape(meta["updated"])}</div>'
            "</article>"
        )
    body = "\n".join(cards) or '<div class="empty">No report HTML files found.</div>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy Report Index</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f7fa; color: #16202a; }}
main {{ max-width: 980px; margin: 0 auto; padding: 48px 28px; }}
h1 {{ margin: 0 0 8px; color: #1a237e; font-size: 40px; letter-spacing: 0; }}
.sub {{ margin: 0 0 28px; color: #657181; font-size: 20px; }}
.card {{ background: #fff; border: 1px solid #e6ebf1; border-radius: 10px; padding: 20px 24px; margin-bottom: 14px; box-shadow: 0 1px 4px rgba(0,0,0,.07); }}
.title {{ display: block; color: #1a73e8; font-size: 24px; font-weight: 700; text-decoration: none; margin-bottom: 8px; }}
.title:hover {{ text-decoration: underline; }}
.meta, .empty {{ color: #8a95a3; font-size: 15px; }}
</style>
</head>
<body>
<main>
<h1>刀具行情报告索引</h1>
<p class="sub">共 {len(report_paths)} 份报告 · Generated by WorkBuddy</p>
{body}
</main>
</body>
</html>
"""


def _is_workbuddy_report_html(workbuddy_root: Path, path: Path) -> bool:
    if not path.is_file() or path.name.lower() == "index.html":
        return False
    try:
        parts = path.relative_to(workbuddy_root).parts
    except ValueError:
        return False
    if not parts:
        return False
    top_dir = parts[0]
    is_task_dir = bool(re.match(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$", top_dir))
    is_automation_dir = top_dir.startswith("automation-")
    if not (is_task_dir or is_automation_dir):
        return False
    return "resources" not in parts and "locales" not in parts


def build_workbuddy_indexes(workbuddy_root: Path) -> list[Path]:
    if not workbuddy_root.exists() or not workbuddy_root.is_dir():
        return []
    report_paths = sorted(
        [
            path
            for path in workbuddy_root.rglob("*.html")
            if _is_workbuddy_report_html(workbuddy_root, path)
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    index_dirs = {workbuddy_root}
    index_dirs.update(path.parent for path in report_paths)
    written = []
    for index_dir in sorted(index_dirs):
        output_path = index_dir / "index.html"
        output_path.write_text(_workbuddy_index_html(index_dir, report_paths), encoding="utf-8")
        written.append(output_path)
    return written


def latest_workbuddy_task_dir(workbuddy_root: Path) -> Path | None:
    """Return the newest timestamped WorkBuddy task directory, when present."""
    if not workbuddy_root.exists() or not workbuddy_root.is_dir():
        return None
    candidates = []
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
    for path in workbuddy_root.iterdir():
        if path.is_dir() and pattern.match(path.name):
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def write_workbuddy_visible_report(
    json_path: Path,
    report_path: Path,
    *,
    workbuddy_root: Path,
) -> Path | None:
    """Copy the HTML into the current WorkBuddy-visible task report directory.

    When the source JSON is already inside a WorkBuddy task, keep the sidecar
    next to it. Otherwise publish a sidecar into the latest timestamped task
    directory.
    """
    try:
        sidecar_path = write_workbuddy_sidecar_report(
            json_path,
            report_path,
            workbuddy_root=workbuddy_root,
        )
    except OSError:
        sidecar_path = None
    if sidecar_path is not None:
        return sidecar_path

    task_dir = latest_workbuddy_task_dir(workbuddy_root)
    if task_dir is None:
        return None

    report = load_report(json_path)
    report_id = slugify(report["report_id"])
    output_dir = task_dir / ".workbuddy" / "memory" / "reports"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{report_id}.html"

        source = report_path.read_text(encoding="utf-8")
        source = source.replace("../index.html", _path_href(output_path, workbuddy_root / "index.html"))
        output_path.write_text(source, encoding="utf-8")
        output_path.with_suffix(".json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return output_path


def write_workbuddy_sidecar_report(
    json_path: Path,
    report_path: Path,
    *,
    workbuddy_root: Path,
) -> Path | None:
    if not _is_relative_to(json_path, workbuddy_root):
        return None
    if json_path.parent == workbuddy_root:
        return None
    report = load_report(json_path)
    rtype = report_type(report)
    report_id = slugify(report["report_id"])
    source = report_path.read_text(encoding="utf-8")
    source = source.replace("../index.html", _path_href(json_path.with_suffix(".html"), workbuddy_root / "index.html"))
    output_path = json_path.with_suffix(".html")
    output_path.write_text(source, encoding="utf-8")
    data_path = output_path.with_name(f"{output_path.stem}_data.json")
    data_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Ensure the copied HTML title/meta still reflect the loaded report if the source came
    # from the canonical latest report path with the same structured payload.
    _ = (rtype, report_id)
    return output_path


def markdown_link(path: Path, label: str) -> str:
    return f"[{label}]({path.resolve().as_posix()})"


def open_html_report(path: Path) -> tuple[bool, str]:
    """Open an HTML report with the OS default handler, returning status text."""
    try:
        resolved = path.resolve()
        if os.name == "nt":
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        else:
            webbrowser.open(resolved.as_uri())
        return True, ""
    except Exception as exc:  # pragma: no cover - depends on desktop shell state.
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_report", nargs=1, help="Path to a report JSON file generated by the agent.")
    parser.add_argument(
        "--no-chat-links",
        action="store_true",
        help="Do not print Markdown links intended to be pasted into the chat.",
    )
    parser.add_argument(
        "--workbuddy-root",
        type=Path,
        default=Path.home() / "WorkBuddy",
        help="WorkBuddy artifact root whose report index should be refreshed when it exists.",
    )
    parser.add_argument(
        "--no-workbuddy-index",
        action="store_true",
        help="Do not refresh WorkBuddy root/task-directory report indexes.",
    )
    parser.add_argument(
        "--open-html",
        action="store_true",
        help="Open the generated HTML report with the OS default browser.",
    )
    args = parser.parse_args()

    for directory in (REPORT_DIR, LATEST_DIR, DATA_DIR, SNAPSHOT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    json_path = Path(args.json_report[0])
    if not json_path.is_absolute():
        json_path = ROOT / json_path
    if not json_path.exists():
        available = sorted(path.name for path in REPORT_DIR.glob("*.json"))
        available.extend(f"data/{path.name}" for path in sorted(DATA_DIR.glob("*.latest.json")))
        print(f"Report JSON not found: {json_path}")
        if available:
            print("Available report JSON files:")
            for name in available:
                print(f"  - {name}")
        else:
            print(f"No report JSON files found in: {REPORT_DIR}")
        print("Tip: save the agent output JSON into .workbuddy/memory/reports/ first, then pass that path to this script.")
        return 1
    report_path, latest_json_path, snapshot_path = build_report(json_path)
    sidecar_path = None
    workbuddy_index_paths: list[Path] = []
    if not args.no_workbuddy_index and args.workbuddy_root.exists():
        sidecar_path = write_workbuddy_visible_report(
            json_path,
            report_path,
            workbuddy_root=args.workbuddy_root,
        )
        workbuddy_index_paths = build_workbuddy_indexes(args.workbuddy_root)
    index_path = build_index()
    print(f"Built report: {report_path}")
    print(f"Wrote latest JSON: {latest_json_path}")
    print(f"Wrote snapshot: {snapshot_path}")
    print(f"Built index:  {index_path}")
    if sidecar_path:
        print(f"Wrote WorkBuddy sidecar report: {sidecar_path}")
    if workbuddy_index_paths:
        print(f"Refreshed WorkBuddy indexes: {len(workbuddy_index_paths)}")
    if args.open_html:
        opened, open_error = open_html_report(sidecar_path or report_path)
        if opened:
            print(f"Opened HTML report: {sidecar_path or report_path}")
        else:
            print(f"Could not open HTML report automatically: {open_error}")
    if not args.no_chat_links:
        print("")
        print("Chat links:")
        print(f"- Open this report: {markdown_link(report_path, report_path.name)}")
        print(f"- Open report index: {markdown_link(index_path, 'index.html')}")
        if sidecar_path:
            print(f"- Open WorkBuddy report: {markdown_link(sidecar_path, sidecar_path.name)}")
        if workbuddy_index_paths:
            print(f"- Open WorkBuddy index: {markdown_link(args.workbuddy_root / 'index.html', 'WorkBuddy index.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
