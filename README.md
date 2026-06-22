# Cutting Tool Price Oracle

Current workflow:

1. Fetch today's spot price for the target metal.
2. Validate the price with multiple sources when needed.
3. Collect news, supply-chain events, international price signals, demand data, and policy context.
4. Make a human/LLM D+1 direction and range judgment.
5. Store the record as `.workbuddy/memory/backtest/predictions/{METAL}_{YYYY-MM-DD}_1d.json`.
6. On D+1, fetch the actual spot price, calculate actual movement, verify direction/range hit, explain misses, and append learnings.
