# Cutting Tool Price Oracle

WorkBuddy expert-team workflow for cutting-tool raw-material market tracking,
D+1 metal volatility prediction, report publishing, and next-day backtesting.

The repository is intentionally split into:

- **Executable orchestration** in `scripts/`
- **Expert-team prompts and skills** in `agents/` and `skills/`
- **Schemas and static references** in `references/`
- **Runtime memory** under `.workbuddy/memory/`

Runtime memory is generated state. Keep the directory skeleton, but do not commit
prices, predictions, backtest results, model outputs, signals, or generated
reports.

## Daily Workflow

Run the default daily loop:

```powershell
py scripts\daily_workbuddy_run.py --date YYYY-MM-DD
```

The default loop performs:

1. Review yesterday's registered predictions.
2. Read cached D+1 actual prices.
3. Write verification results when actual prices are available.
4. Generate WorkBuddy tasks for missing price fetches, bias review, headline
   writing, and prediction work.
5. Read current learnings.
6. Write today's predictions when model output or signal files are available.
7. Return a manifest with the next phase and expected files.

The WorkBuddy expert team can call large models freely. The scripts therefore
do not try to replace every expert judgment with deterministic code. They
provide the file contracts, validation, persistence, and report publishing
around the expert outputs.

## Phases

Verify yesterday's predictions:

```powershell
py scripts\daily_workbuddy_run.py --phase verify --date YYYY-MM-DD
```

Generate or register today's predictions:

```powershell
py scripts\daily_workbuddy_run.py --phase predict --date YYYY-MM-DD
```

Publish the daily briefing report:

```powershell
py scripts\daily_workbuddy_run.py --phase report --date YYYY-MM-DD
```

Use `--metals AL,CU,NI` to limit the metal set. Use `--json-only` when another
tool only needs the machine-readable manifest.

## Runtime Directories

```text
.workbuddy/memory/
  prices/                         # cached spot prices
  signals/                        # expert signal extraction output
  model_outputs/
    bias_review/                  # miss-analysis JSON written by risk-control
    predictions/                  # prediction JSON written by metals-analyst
  backtest/
    alerts/
    learnings/
    predictions/
    results/
  reports/
    latest/
    data/
      snapshots/
```

Only `.gitkeep` files should remain in these folders in a clean project checkout.

## Key File Contracts

Price cache:

```text
.workbuddy/memory/prices/{METAL}_{YYYY-MM-DD}.json
```

Registered D+1 prediction:

```text
.workbuddy/memory/backtest/predictions/{METAL}_{YYYY-MM-DD}_1d.json
```

Backtest result:

```text
.workbuddy/memory/backtest/results/{METAL}_{YYYY-MM-DD}_1d.json
```

Bias review output:

```text
.workbuddy/memory/model_outputs/bias_review/{METAL}_{YYYY-MM-DD}.json
```

External prediction output:

```text
.workbuddy/memory/model_outputs/predictions/{METAL}_{YYYY-MM-DD}.json
```

Generated reports:

```text
.workbuddy/memory/reports/latest/{REPORT_TYPE}_{TOPIC}.html
.workbuddy/memory/reports/data/{REPORT_TYPE}_{TOPIC}.latest.json
.workbuddy/memory/reports/data/snapshots/{REPORT_TYPE}_{TOPIC}_{DATE}_{REPORT_ID}_{forecast|backtest}.json
```

## Report Types

Every report JSON must declare one `report_type`.

| `report_type` | When to use it | Required focus |
| --- | --- | --- |
| `tool_price` | Cutting-tool price, raw-material cost, quote risk, or purchasing timing | Metals, D+1 outlook, tool cost impact, purchasing recommendation |
| `single_metal` | One metal only | Spot price, D+1 direction/range, confidence, rationale |
| `daily_briefing` | Daily market note | Multi-metal spot view, news/supply-chain sections, tomorrow outlook |
| `weekly_briefing` | Weekly/monthly summary | Registered predictions, verified results, hit rates, learnings |
| `backtest` | Accuracy check | Prior prediction, actual price, direction/range hit, error, bias reason |

The schema lives at `references/report-schema.json`.

## Report Publishing

Build an HTML report from a valid report JSON:

```powershell
py scripts\build_html_report.py path\to\report.json
```

If you do not want to refresh `%USERPROFILE%\WorkBuddy`, pass:

```powershell
py scripts\build_html_report.py path\to\report.json --no-workbuddy-index
```

## Validation

Run local smoke checks without touching real `.workbuddy` state:

```powershell
py -B scripts\validate_closed_loop.py
```

The validation patches all runtime paths into a temporary directory, so it is
safe to run in a clean project checkout.
