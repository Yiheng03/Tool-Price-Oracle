---
name: next-day-volatility-tracker
description: Record and verify one-day metal volatility predictions. On day D, stores current spot price, news summary, D+1 direction and range. On D+1, fetches actual spot price, checks direction/range hit, records error and bias learnings.
---

# 次日波动跟踪器

本 Skill 负责单日循环：

```
D 日：现货价 + 新闻 → 判断 D+1 涨跌方向和幅度区间 → 记录
D+1 日：获取实际价 → 计算实际波动 → 判断是否命中 → 分析偏差 → 沉淀经验
```

所有记录围绕 D 日到 D+1 日的单日波动，文件名使用 `_1d.json`。

## 存储路径

```
.workbuddy/memory/backtest/
├── predictions/
│   └── {METAL}_{YYYY-MM-DD}_1d.json
├── results/
│   └── {METAL}_{YYYY-MM-DD}_1d.json
├── learnings/
│   └── {METAL}_learnings.md
└── alerts/
```

## today_predict

输入：金属代码（如 `CU`）、当日日期、当日现货价、新闻摘要、供应链摘要、分析师给出的 D+1 方向和幅度区间。

流程：

1. 调用 `spot-price-fetcher` 获取当日现货价。
2. 必要时调用 `multi-source-validator` 校验报价。
3. 调用 `news-impact-mapper`、`supply-chain-scanner`、`global-price-signal-detector` 收集影响因子。
4. 由 analyst 给出 D+1 判断：`up`、`down`、`flat`，以及幅度区间，例如 `+1%~+3%`、`-1%~0%`、`-0.5%~+0.5%`。
5. 写入 `predictions/{METAL}_{YYYY-MM-DD}_1d.json`。

预测记录格式：

```json
{
  "metal": "CU",
  "date_d": "2026-06-22",
  "target_date": "2026-06-23",
  "price_d": 78500,
  "price_unit": "CNY/ton",
  "price_source": "SMM",
  "predicted_direction": "up",
  "predicted_range_pct": [0.01, 0.03],
  "confidence": "medium",
  "news_summary": [
    "智利铜矿供应扰动扩大",
    "美元走弱支撑工业金属"
  ],
  "supply_chain_summary": [
    "TC/RC 低位，冶炼端供应偏紧"
  ],
  "rationale": "供应端扰动和美元走弱同向，预计次日偏强。",
  "status": "pending"
}
```

## tomorrow_verify

输入：金属代码、D+1 实际现货价。

流程：

1. 读取 `predictions/{METAL}_{D}_1d.json`。
2. 调用 `spot-price-fetcher` 获取 D+1 实际价。
3. 计算实际波动：

```text
actual_change_pct = (price_d_plus_1 - price_d) / price_d
```

4. 判断方向命中：

```text
predicted up   -> actual_change_pct > 0
predicted down -> actual_change_pct < 0
predicted flat -> abs(actual_change_pct) <= flat_threshold
```

默认 `flat_threshold = 0.005`（0.5%）。

5. 判断区间命中：`actual_change_pct` 是否落在 `predicted_range_pct` 内。
6. 写入 `results/{METAL}_{D+1}_1d.json`。

结果格式：

```json
{
  "metal": "CU",
  "prediction_date": "2026-06-22",
  "actual_date": "2026-06-23",
  "price_d": 78500,
  "price_d_plus_1": 80100,
  "actual_change_pct": 0.0204,
  "predicted_direction": "up",
  "predicted_range_pct": [0.01, 0.03],
  "actual_direction": "up",
  "hit_direction": true,
  "hit_range": true,
  "error_pct": 0.0004,
  "actual_price_source": "SMM",
  "bias_reason": "",
  "status": "verified"
}
```

## bias_review

当方向或区间未命中时执行：

1. 读取 D 日预测记录的 `news_summary`、`supply_chain_summary`、`rationale`。
2. 收集 D+1 新进新闻和实际价格变化。
3. 给出 2~3 条偏差原因，常见类型包括：
   - 突发事件：D 日之后发生新事件。
   - 新闻解读偏差：事件方向判断对，但影响幅度高估或低估。
   - 市场提前定价：新闻已被价格消化。
   - 宏观反向：美元、利率、库存或期货资金面抵消基本面。
   - 报价源差异：不同现货报价口径不一致。
4. 追加到 `learnings/{METAL}_learnings.md`。

经验格式：

```markdown
## 2026-06-23 | CU | Missed Range

- Prediction: up +1%~+3%
- Actual: up +0.4%
- Main reason: 利多新闻已提前反映在 D 日价格中，次日追涨不足。
- Next adjustment: 遇到已连续上涨超过 3% 的金属，次日看涨区间降一档。
```

## 使用约束

- 跟踪窗口为 1 天。
- 文件名必须以 `_1d.json` 结尾。
- 记录中必须保留当日价、实际价、方向命中、区间命中、误差和偏差原因。
