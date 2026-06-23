# Cutting Tool Price Oracle

Current workflow:

1. Fetch today's spot price for the target metal.
2. Validate the price with multiple sources when needed.
3. Collect news, supply-chain events, international price signals, demand data, and policy context.
4. Make a human/LLM D+1 direction and range judgment.
5. Store the record as `.workbuddy/memory/backtest/predictions/{METAL}_{YYYY-MM-DD}_1d.json`.
6. On D+1, fetch the actual spot price, calculate actual movement, verify direction/range hit, explain misses, and append learnings.

## Architecture

This project combines two layers:

**Executable code** (`scripts/build_html_report.py`): validates report JSON against
`references/report-schema.json`, generates typed HTML reports, writes latest JSON and
timestamped snapshots, and rebuilds the static report index. This is the only automated
pipeline — run it after an agent produces a report JSON.

**Agent/skill instructions** (`agents/*.md`, `skills/*/SKILL.md`): these define the
analysis workflow (spot price fetching, multi-source validation, news scanning, supply
chain intelligence, cost modeling, risk control, backtest registration) as LLM agent
prompts. They are not executable scripts — a human or LLM orchestrator (e.g. WorkBuddy)
must follow them step by step to produce the report JSON that feeds into the HTML
pipeline.

To close the automation gap, you would need to add scripts or tools that call price
APIs, validate sources, and register backtests programmatically, rather than relying
solely on LLM instruction-following.

## Report types

Every report JSON must declare one `report_type`. Different report types are
stored and rendered separately so their content is not forced into one layout.

| `report_type` | When to use it | Required focus |
| --- | --- | --- |
| `tool_price` | User asks about cutting-tool price, raw-material cost, quote risk, or purchasing timing. | Metals, D+1 outlook, tool cost impact, purchasing recommendation. |
| `single_metal` | User asks about one metal only. | One metal's spot price, D+1 direction/range, confidence, rationale. |
| `daily_briefing` | User asks for today's market, daily briefing, morning note, or similar. | Multi-metal spot view, news/supply-chain sections, tomorrow outlook. |
| `weekly_briefing` | User asks for weekly/monthly summary. | Registered predictions, verified results, hit rates, and learnings. |
| `backtest` | User asks whether a prediction was accurate, or requests a backtest/accuracy check. | Prior prediction, actual price, direction/range hit, error, bias reason. |

Common fields are defined in `references/report-schema.json`. Type-specific
content should be carried by dedicated fields such as `tool_cost_impact`,
`recommendation`, `backtests`, and `sections`.

## Report storage

HTML reports are treated as generated views. Keep only the latest HTML for each
topic, and keep historical records as JSON snapshots:

```text
.workbuddy/memory/reports/
  index.html
  latest/
    {REPORT_TYPE}_{TOPIC}.html
  data/
    {REPORT_TYPE}_{TOPIC}.latest.json
    snapshots/
      {REPORT_TYPE}_{TOPIC}_{prediction_date}_{report_id}_forecast.json
      {REPORT_TYPE}_{TOPIC}_{actual_date}_{report_id}_backtest.json
```

Storage rules:

1. Latest HTML is written to `.workbuddy/memory/reports/latest/{REPORT_TYPE}_{TOPIC}.html` and may be overwritten.
2. Latest structured JSON is written to `.workbuddy/memory/reports/data/{REPORT_TYPE}_{TOPIC}.latest.json` and may be overwritten.
3. Forecast snapshots are written to `.workbuddy/memory/reports/data/snapshots/{REPORT_TYPE}_{TOPIC}_{prediction_date}_{report_id}_forecast.json`.
4. Backtest snapshots are written to `.workbuddy/memory/reports/data/snapshots/{REPORT_TYPE}_{TOPIC}_{actual_date}_{report_id}_backtest.json`.
5. JSON should carry explicit date fields such as `prediction_date`, `target_date`, `actual_date`, and `updated_at` when applicable.

## HTML report generation

Route B is implemented as a small static publishing pipeline:

1. The agent produces a report JSON matching `references/report-schema.json`.
2. The agent immediately runs `scripts/build_html_report.py` for that JSON.
3. The same script writes latest JSON, writes one snapshot, and refreshes `.workbuddy/memory/reports/index.html`.
4. When `%USERPROFILE%\WorkBuddy` exists, the script also refreshes the root
   WorkBuddy `index.html` and each report task folder's local `index.html`, so
   reports opened from WorkBuddy task folders can return to a complete report
   center.
5. The agent sends the Markdown links printed under `Chat links:` back to the user, so the user can choose which HTML page to open.

Generate a report from an agent-produced JSON file:

```powershell
py scripts\build_html_report.py path\to\report.json
```

If you are rebuilding only the repository report center and do not want to
touch WorkBuddy's artifact folder, pass:

```powershell
py scripts\build_html_report.py path\to\report.json --no-workbuddy-index
```

Open:

```text
.workbuddy/memory/reports/index.html
.workbuddy/memory/reports/latest/{REPORT_TYPE}_{TOPIC}.html
```

When a chat response includes a generated report, include both links:

```markdown
- Open this report: [tool_price_CU.html](D:/Junepj/tool/.workbuddy/memory/reports/latest/tool_price_CU.html)
- Open report index: [index.html](D:/Junepj/tool/.workbuddy/memory/reports/index.html)
```
