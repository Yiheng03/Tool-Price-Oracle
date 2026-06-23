# Cutting Tool Price Oracle

Current workflow:

1. Fetch today's spot price for the target metal.
2. Validate the price with multiple sources when needed.
3. Collect news, supply-chain events, international price signals, demand data, and policy context.
4. Make a human/LLM D+1 direction and range judgment.
5. Store the record as `.workbuddy/memory/backtest/predictions/{METAL}_{YYYY-MM-DD}_1d.json`.
6. On D+1, fetch the actual spot price, calculate actual movement, verify direction/range hit, explain misses, and append learnings.

## Report storage

HTML reports are treated as generated views. Keep only the latest HTML for each
topic, and keep historical records as JSON snapshots:

```text
.workbuddy/memory/reports/
  index.html
  latest/
    {TOPIC}.html
  data/
    {TOPIC}.latest.json
    snapshots/
      {TOPIC}_{prediction_date}_forecast.json
      {TOPIC}_{actual_date}_backtest.json
```

Storage rules:

1. Latest HTML is written to `.workbuddy/memory/reports/latest/{TOPIC}.html` and may be overwritten.
2. Latest structured JSON is written to `.workbuddy/memory/reports/data/{TOPIC}.latest.json` and may be overwritten.
3. Forecast snapshots are written to `.workbuddy/memory/reports/data/snapshots/{TOPIC}_{prediction_date}_forecast.json`.
4. Backtest snapshots are written to `.workbuddy/memory/reports/data/snapshots/{TOPIC}_{actual_date}_backtest.json`.
5. JSON should carry explicit date fields such as `prediction_date`, `target_date`, `actual_date`, and `updated_at` when applicable.

## HTML report generation

Route B is implemented as a small static pipeline:

1. The agent produces a report JSON matching `references/report-schema.json`.
2. `scripts/build_html_report.py` embeds that JSON into the topic's latest HTML.
3. The same script writes latest JSON, writes one snapshot, and refreshes `.workbuddy/memory/reports/index.html`.

Generate a report from an agent-produced JSON file:

```powershell
py scripts\build_html_report.py path\to\report.json
```

Open:

```text
.workbuddy/memory/reports/index.html
.workbuddy/memory/reports/latest/{TOPIC}.html
```
