#!/usr/bin/env python3
"""Build typed standalone HTML reports and a static report index from report JSON."""

from __future__ import annotations

import argparse
import html
import json
import re
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


REPORT_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #16202a;
      --muted: #657181;
      --line: #dfe5ec;
      --accent: #0f766e;
      --accent-2: #9a3412;
      --danger: #b91c1c;
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
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    main { padding: 24px min(5vw, 56px) 44px; }
    h1 { margin: 0 0 8px; font-size: 28px; line-height: 1.2; letter-spacing: 0; }
    h2 { margin: 0 0 14px; font-size: 17px; letter-spacing: 0; }
    h3 { margin: 16px 0 8px; font-size: 15px; letter-spacing: 0; }
    p { line-height: 1.6; }
    .meta, .muted { color: var(--muted); }
    .grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 16px;
      align-items: stretch;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }
    .span-12 { grid-column: span 12; }
    .span-7 { grid-column: span 7; }
    .span-5 { grid-column: span 5; }
    .metric {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 10px;
    }
    .metric div {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 78px;
    }
    .metric strong {
      display: block;
      font-size: 24px;
      margin-top: 8px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      overflow: hidden;
      border-radius: 8px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }
    th { color: var(--muted); font-weight: 650; }
    tr:last-child td { border-bottom: 0; }
    ul { margin: 8px 0 0; padding-left: 20px; }
    li { margin: 6px 0; line-height: 1.5; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 9px;
      border-radius: 999px;
      background: #e8f5f2;
      color: var(--accent);
      font-size: 13px;
      font-weight: 650;
    }
    .pill.down, .pill.missed, .pill.high { background: #fef2f2; color: var(--danger); }
    .pill.flat, .pill.medium { background: #eef2f7; color: #475569; }
    .pill.volatile, .pill.partial { background: #fff7ed; color: var(--accent-2); }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }
    button, a.button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      border-radius: 8px;
      padding: 9px 12px;
      font: inherit;
      cursor: pointer;
      text-decoration: none;
    }
    button.primary, a.button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }
    pre {
      max-height: 360px;
      overflow: auto;
      background: #111827;
      color: #e5e7eb;
      border-radius: 8px;
      padding: 14px;
      font-size: 13px;
      line-height: 1.5;
    }
    @media (max-width: 780px) {
      .span-7, .span-5 { grid-column: span 12; }
      .metric { grid-template-columns: 1fr; }
      th, td { font-size: 13px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="meta">__TYPE_LABEL__ / __DATE__ / __REPORT_ID__</div>
    <h1>__TITLE__</h1>
    <p>__SUMMARY__</p>
    <div class="actions">
      <a class="button" href="__INDEX_HREF__">Back to report center</a>
      <button class="primary" id="download-json">Download JSON</button>
      <button id="toggle-json">Show JSON</button>
    </div>
  </header>
  <main class="grid" id="report-root"></main>
  <script type="application/json" id="report-data">__REPORT_JSON__</script>
  <script>
    const report = JSON.parse(document.getElementById("report-data").textContent);
    const root = document.getElementById("report-root");
    const reportTypes = __REPORT_TYPES_JSON__;
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
    const fmtPct = (range) => {
      if (!Array.isArray(range) || range.length < 2) return "";
      return `${(Number(range[0]) * 100).toFixed(2)}% to ${(Number(range[1]) * 100).toFixed(2)}%`;
    };
    const fmtMoney = (value, currency = "") => {
      if (value === undefined || value === null || value === "") return "";
      return `${currency} ${Number(value).toFixed(2)}`.trim();
    };
    const panel = (title, body, span = 12) => `
      <section class="panel span-${span}">
        <h2>${escapeHtml(title)}</h2>
        ${body}
      </section>
    `;
    const rows = (items, columns) => items.map((item) => `
      <tr>${columns.map(([key]) => `<td>${escapeHtml(item?.[key] ?? "")}</td>`).join("")}</tr>
    `).join("");
    const table = (items, columns) => {
      if (!Array.isArray(items) || items.length === 0) return '<p class="muted">No entries.</p>';
      return `
        <table>
          <thead><tr>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead>
          <tbody>${rows(items, columns)}</tbody>
        </table>
      `;
    };
    const metalTable = () => {
      const metals = report.metals || [];
      if (!Array.isArray(metals) || metals.length === 0) return '<p class="muted">No entries.</p>';
      return `
        <table>
          <thead>
            <tr>
              <th>Metal</th>
              <th>Spot</th>
              <th>D+1</th>
              <th>Range</th>
              <th>Confidence</th>
              <th>Rationale</th>
            </tr>
          </thead>
          <tbody>
            ${metals.map((metal) => `
              <tr>
                <td><strong>${escapeHtml(metal.code)}</strong><br><span class="muted">${escapeHtml(metal.name)}</span></td>
                <td>${escapeHtml(Number(metal.spot_price).toLocaleString())}<br><span class="muted">${escapeHtml(metal.unit)}</span></td>
                <td><span class="pill ${escapeHtml(metal.direction)}">${escapeHtml(metal.direction)}</span></td>
                <td>${escapeHtml(fmtPct(metal.range_pct))}</td>
                <td>${escapeHtml(metal.confidence)}</td>
                <td>${escapeHtml(metal.rationale)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
    };
    const recommendationPanel = () => {
      if (!report.recommendation) return "";
      return panel("Recommendation", `
        <p><span class="pill ${escapeHtml(report.recommendation.risk_level)}">${escapeHtml(report.recommendation.risk_level)}</span></p>
        <p><strong>${escapeHtml(report.recommendation.action)}</strong></p>
        <p class="muted">${escapeHtml(report.recommendation.reason)}</p>
      `);
    };
    const costPanel = () => {
      const impact = report.tool_cost_impact;
      if (!impact) return "";
      return panel("Tool Cost Impact", `
        <p class="muted">${escapeHtml(impact.tool_spec)}</p>
        <div class="metric">
          <div><span class="muted">Base</span><strong>${escapeHtml(fmtMoney(impact.base_cost, impact.currency))}</strong></div>
          <div><span class="muted">Low</span><strong>${escapeHtml(fmtMoney(impact.next_day_low, impact.currency))}</strong></div>
          <div><span class="muted">High</span><strong>${escapeHtml(fmtMoney(impact.next_day_high, impact.currency))}</strong></div>
        </div>
        <p><strong>Main driver:</strong> ${escapeHtml(impact.main_driver)}</p>
      `, 5);
    };
    const backtestPanel = () => {
      const yesterday = report.json_payload?.yesterday_backtest;
      const items = report.backtests || report.json_payload?.backtests || (yesterday ? [yesterday] : []);
      if (!Array.isArray(items) || items.length === 0) return "";
      return panel("Backtest Results", table(items, [
        ["metal", "Metal"],
        ["prediction_date", "Prediction Date"],
        ["actual_date", "Actual Date"],
        ["predicted_direction", "Predicted"],
        ["actual_direction", "Actual"],
        ["hit_direction", "Direction Hit"],
        ["hit_range", "Range Hit"],
        ["bias_reason", "Bias Reason"],
        ["next_adjustment", "Next Adjustment"],
      ]));
    };
    const yesterdayReviewPanel = () => {
      const yr = report.yesterday_review;
      if (!yr || typeof yr !== "object") return "";
      const backtests = Array.isArray(yr.backtests) ? yr.backtests : [];
      const total = backtests.length;
      const hits = backtests.filter((bt) => bt.hit_direction).length;
      const hitRate = total > 0 ? ((hits / total) * 100).toFixed(1) : "0.0";
      const hitClass = total > 0 && hits / total >= 0.5 ? "" : "missed";
      let body = `
        <div style="background:#fffbeb;border-left:4px solid #f59e0b;padding:14px 18px;border-radius:6px;margin-bottom:16px;">
          <div style="font-size:20px;font-weight:700;color:#92400e;margin-bottom:6px;">${escapeHtml(yr.headline || "")}</div>
          <div style="font-size:15px;color:#657181;">方向命中率：<strong style="font-size:22px;" class="pill ${hitClass}">${hitRate}%</strong> &nbsp;（${hits}/${total}）</div>
        </div>
      `;
      if (yr.summary) {
        body += `<p style="font-size:15px;line-height:1.7;">${escapeHtml(yr.summary)}</p>`;
      }
      if (Array.isArray(yr.learnings_written) && yr.learnings_written.length > 0) {
        body += `<h3>经验记录</h3><ul>${yr.learnings_written.map((l) => `<li>${escapeHtml(typeof l === "string" ? l : JSON.stringify(l))}</li>`).join("")}</ul>`;
      }
      if (backtests.length > 0) {
        body += `<h3>逐项复核</h3>
        <table>
          <thead><tr>
            <th>品种</th><th>预测方向</th><th>实际方向</th><th>命中</th><th>偏差原因</th><th>调整规则</th>
          </tr></thead>
          <tbody>
            ${backtests.map((bt) => `
              <tr>
                <td><strong>${escapeHtml(bt.metal || "")}</strong></td>
                <td><span class="pill ${escapeHtml(bt.predicted_direction || "")}">${escapeHtml(bt.predicted_direction || "")}</span></td>
                <td><span class="pill ${escapeHtml(bt.actual_direction || "")}">${escapeHtml(bt.actual_direction || "")}</span></td>
                <td><span class="pill ${bt.hit_direction ? "" : "missed"}">${bt.hit_direction ? "✓ 命中" : "✗ 未中"}</span></td>
                <td>${escapeHtml(bt.bias_reason || "")}</td>
                <td>${escapeHtml(bt.next_adjustment || "")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>`;
      }
      return panel("昨日预测复盘", body, 12);
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
        return panel(section.title || "Details", body || '<p class="muted">No details.</p>');
      }).join("");
    };
    const type = report.report_type || "tool_price";
    const typeDescription = reportTypes[type]?.description || "";
    let body = yesterdayReviewPanel();
    body += panel("Report Type", `<p>${escapeHtml(typeDescription)}</p>`, 12);
    if (type === "tool_price") {
      body += panel("Metal Signals", metalTable(), report.tool_cost_impact ? 7 : 12);
      body += costPanel();
      body += recommendationPanel();
    } else if (type === "single_metal") {
      body += panel("Single Metal Outlook", metalTable(), 12);
      body += recommendationPanel();
    } else if (type === "daily_briefing" || type === "weekly_briefing") {
      body += panel("Metal Briefing", metalTable(), 12);
      body += sectionsPanel();
      body += recommendationPanel();
    } else if (type === "backtest") {
      body += backtestPanel();
      body += sectionsPanel();
      body += recommendationPanel();
    } else {
      body += panel("Metal Signals", metalTable(), 12);
      body += sectionsPanel();
      body += recommendationPanel();
    }
    body += panel("Embedded JSON", '<pre id="json-view"></pre>', 12).replace('<section', '<section id="json-panel" hidden');
    root.innerHTML = body;
    document.getElementById("json-view").textContent = JSON.stringify(report, null, 2);
    document.getElementById("toggle-json").addEventListener("click", () => {
      const panel = document.getElementById("json-panel");
      panel.hidden = !panel.hidden;
      document.getElementById("toggle-json").textContent = panel.hidden ? "Show JSON" : "Hide JSON";
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


def report_type(report: dict[str, Any], *, legacy_infer_type: bool = False) -> str:
    value = str(report.get("report_type") or "").strip()
    if value in REPORT_TYPES:
        return value
    if legacy_infer_type:
        if is_backtest_report(report):
            return "backtest"
        if len(report.get("metals", [])) == 1 and not report.get("tool_cost_impact"):
            return "single_metal"
        return "tool_price"
    valid = ", ".join(sorted(REPORT_TYPES))
    if not value:
        raise ValueError(
            f"report is missing required key: report_type. "
            f"Valid values: {valid}"
        )
    raise ValueError(
        f"unknown report_type {value!r}. Valid values: {valid}"
    )


def load_report(path: Path, *, legacy_infer_type: bool = False) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    missing = [
        key
        for key in ("report_id", "date", "title", "summary", "metals")
        if key not in report
    ]
    if missing:
        raise ValueError(f"{path} is missing required keys: {', '.join(missing)}")
    rtype = report_type(report, legacy_infer_type=legacy_infer_type)
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


def build_report(json_path: Path, *, legacy_infer_type: bool = False) -> tuple[Path, Path, Path]:
    report = load_report(json_path, legacy_infer_type=legacy_infer_type)
    report, latest_json_path, snapshot_path, topic = persist_report_data(report)
    rtype = report_type(report, legacy_infer_type=legacy_infer_type)
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
        .replace("__REPORT_TYPES_JSON__", json.dumps(REPORT_TYPES, ensure_ascii=False))
    )
    output_path.write_text(rendered, encoding="utf-8")
    return output_path, latest_json_path, snapshot_path


def build_index(*, legacy_infer_type: bool = False) -> Path:
    reports = []
    for json_path in sorted(DATA_DIR.glob("*.latest.json")):
        report = load_report(json_path, legacy_infer_type=legacy_infer_type)
        report_id = slugify(report["report_id"])
        rtype = report_type(report, legacy_infer_type=legacy_infer_type)
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


def build_workbuddy_indexes(workbuddy_root: Path) -> list[Path]:
    if not workbuddy_root.exists() or not workbuddy_root.is_dir():
        return []
    report_paths = sorted(
        [
            path
            for path in workbuddy_root.glob("*/report_*.html")
            if path.is_file() and "resources" not in path.parts
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


def write_workbuddy_sidecar_report(
    json_path: Path,
    report_path: Path,
    *,
    workbuddy_root: Path,
    legacy_infer_type: bool = False,
) -> Path | None:
    if not _is_relative_to(json_path, workbuddy_root):
        return None
    if json_path.parent == workbuddy_root:
        return None
    report = load_report(json_path, legacy_infer_type=legacy_infer_type)
    rtype = report_type(report, legacy_infer_type=legacy_infer_type)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_report", nargs=1, help="Path to a report JSON file generated by the agent.")
    parser.add_argument(
        "--no-chat-links",
        action="store_true",
        help="Do not print Markdown links intended to be pasted into the chat.",
    )
    parser.add_argument(
        "--legacy-infer-type",
        action="store_true",
        help="Infer report_type for legacy JSON files that lack a valid report_type field.",
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
    report_path, latest_json_path, snapshot_path = build_report(json_path, legacy_infer_type=args.legacy_infer_type)
    sidecar_path = None
    workbuddy_index_paths: list[Path] = []
    if not args.no_workbuddy_index and args.workbuddy_root.exists():
        sidecar_path = write_workbuddy_sidecar_report(
            json_path,
            report_path,
            workbuddy_root=args.workbuddy_root,
            legacy_infer_type=args.legacy_infer_type,
        )
        workbuddy_index_paths = build_workbuddy_indexes(args.workbuddy_root)
    index_path = build_index(legacy_infer_type=args.legacy_infer_type)
    print(f"Built report: {report_path}")
    print(f"Wrote latest JSON: {latest_json_path}")
    print(f"Wrote snapshot: {snapshot_path}")
    print(f"Built index:  {index_path}")
    if sidecar_path:
        print(f"Wrote WorkBuddy sidecar report: {sidecar_path}")
    if workbuddy_index_paths:
        print(f"Refreshed WorkBuddy indexes: {len(workbuddy_index_paths)}")
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
