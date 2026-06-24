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

输入：金属代码（如 `CU`）、当日日期、当日现货价、新闻摘要、供应链摘要、国际信号、历史 learnings。

### 数据收集

1. 调用 `spot-price-fetcher` 获取当日现货价。
2. 必要时调用 `multi-source-validator` 校验报价。
3. 调用 `news-impact-mapper`、`supply-chain-scanner`、`global-price-signal-detector`、`demand-sensor` 收集影响因子。

### 信号提取（先于预测合成）

**必须先从所有数据源提取结构化信号，再进入预测合成。** 每条信号是一个 JSON 对象：

```json
{
  "source": "news|supply_chain|international|demand|macro",
  "event": "简短描述（≤20字）",
  "direction": "up|down|neutral",
  "strength": "high|medium|low",
  "timing": "D-day|overnight|ongoing",
  "brief": "1行展示用摘要"
}
```

**强度校准：**
- high：多源交叉验证 或 可直接观察到的价格影响
- medium：可信单一来源 + 逻辑因果链清晰
- low：推测性、传闻性或间接影响

**来源覆盖：**
- `news-impact-mapper` → source="news"
- `supply-chain-scanner` → source="supply_chain"
- `global-price-signal-detector` → source="international"
- `demand-sensor` → source="demand"
- 宏观（美元/利率/库存）→ source="macro"

每个金属输出 3-8 条信号。

### 预测合成

**基于结构化信号逐一分析：**

**a) 方向** — 按强度加权计分（high=3, medium=2, low=1）：
- 净得分 ≥4 → up，≤-4 → down，否则 → flat
- 如有 timing="overnight" 的信号，标记为潜在反转风险

**b) 区间** — 从默认 [-1.5%, +1.5%] 开始：
- ≥3 条 high 强度同向：缩窄至 [0%, +2%] 或 [-2%, 0%]
- 混合强度或冲突：保持默认或扩宽至 [-2%, +2%]
- 有 overnight 信号：两侧各扩 0.5%
- 叠加 learnings 规则调整

**c) 置信度**：
- high：≥3 条 strength≥medium 信号一致，无冲突，无 overnight 事件风险
- medium：≥2 条信号一致，轻微冲突
- low：信号冲突或仅单一来源

**d) 理由** — 点名驱动方向的 top 2 信号：
- 格式：`[信号1] + [信号2] → [方向判断], 但[主要风险]。`

**e) used_learnings** — 列出实际应用的每条学习规则描述

### 输出格式

先输出预测 JSON，再写入文件：

```json
{
  "predicted_direction": "up|down|flat|volatile",
  "predicted_range_pct": [low, high],
  "confidence": "high|medium|low",
  "rationale": "1-sentence synthesis",
  "used_learnings": ["rule description 1", "..."]
}
```

### 写入预测记录

将上述 JSON 字段加上元数据和信号数组写入 `predictions/{METAL}_{YYYY-MM-DD}_1d.json`：

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
  "signals": [
    {"source": "news", "event": "智利铜矿罢工扩大", "direction": "up", "strength": "high", "timing": "D-day", "brief": "智利铜矿供应扰动扩大"},
    {"source": "macro", "event": "美元指数走弱", "direction": "up", "strength": "medium", "timing": "ongoing", "brief": "美元走弱支撑工业金属"},
    {"source": "supply_chain", "event": "TC/RC低位", "direction": "up", "strength": "medium", "timing": "ongoing", "brief": "TC/RC低位，冶炼端供应偏紧"}
  ],
  "rationale": "供应端扰动和美元走弱同向，预计次日偏强。",
  "used_learnings": ["谈判类利好未跟随时看涨降档"],
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

当方向或区间未命中时执行。**必须输出结构化 JSON**，不可只给模糊描述。

### 输入收集

1. 读取 D 日预测记录的 `news_summary`、`supply_chain_summary`、`rationale`、`signals`。
2. 获取 D+1 实际价格变化：`actual_change_pct = (price_d_plus_1 - price_d) / price_d`。
3. 调用 `news-impact-mapper` 收集 D 日收盘后到 D+1 日的新进新闻（隔夜/日内突发事件）。
4. 检查宏观反向信号：美元指数、利率、库存数据、期货持仓变化。

### 偏差分析清单（逐项检查）

| 类别 | 检查问题 |
|------|---------|
| 突发事件 | D 日收盘后是否发生了预测时未知的新事件？ |
| 新闻解读偏差 | 事件方向判断对，但影响幅度高估或低估了多少？ |
| 市场提前定价 | D 日新闻是否已被当日价格充分消化，次日无追涨/杀跌动力？ |
| 宏观反向 | 美元、利率、库存或期货资金面是否抵消了基本面信号？ |
| 报价源差异 | 不同现货报价口径是否不一致导致基准价偏差？ |

### 输出格式（必须严格 JSON）

```json
{
  "bias_reason": "1-2句，必须点名具体事件/指标/假设。好的例子: 'D日智利铜矿罢工预期被高估，实际仅影响2%发货量；夜盘美联储鹰派讲话逆转美元走弱趋势，铜价不涨反跌。' 坏的例子: '市场波动超预期。'",
  "next_adjustment": "1条条件→动作规则，必须可被下次预测机械执行。格式: '当[触发条件]时，[具体动作]。'"
}
```

### next_adjustment 规则分类（必须从下列模板中选择最匹配的一类）

每条规则必须写成 **"当…时，…"** 的条件触发格式，确保下次预测可直接套用。

| 类型 | 触发条件示例 | 动作模板 | 好的例子 |
|------|-------------|---------|---------|
| 区间降档 | 连续上涨超X% / 利好已被D日价格消化 | 次日看涨/看跌区间降一档（如 [+1%,+3%] → [0%,+2%]） | 当金属连续2日累计涨超3%时，次日看涨区间降一档，上限减1%。 |
| 区间扩宽 | 重大事件前后 / 波动率突增 | 区间扩至 [−X%,+X%] | 当美联储议息会议前后24小时，区间扩至[-1.5%,+1.5%]。 |
| 方向覆盖 | 特定事件类型必然反向或横盘 | 方向预测改为 flat/volatile | 当D日有谈判类利好但夜盘无跟进时，次日方向改为flat。 |
| 置信度降级 | 信号冲突 / 单一来源 | 置信度从 high→medium 或 medium→low | 当仅有单一新闻源支持看涨且无供应链数据佐证时，置信度降一级。 |
| 信号过滤 | 某类信号反复误导 | 忽略该类信号 / 降低权重 | 当智利铜矿罢工新闻未伴随发货量实际下降数据时，不将其计入看涨信号。 |
| 暂停预测 | 极端不确定性 | 当日不做方向预测，标记为 volatile | 当D日夜盘出现突发地缘冲突且未明朗时，次日暂停预测，方向标记为volatile。 |

**禁止的写法**（不是可执行规则）：
- "更谨慎一些。" — 没有触发条件，没有具体动作
- "注意宏观因素。" — 太模糊，无法执行
- "多看几个数据源。" — 没有说明哪些数据源、何时触发

### 写入动作

1. 将 `bias_reason` 写入 `results/{METAL}_{D}_1d.json` 的 `bias_reason` 字段。
2. 将 `next_adjustment` 写入同一文件的 `next_adjustment` 字段。
3. 追加经验到 `learnings/{METAL}_learnings.md`。

经验格式：

```markdown
## YYYY-MM-DD | METAL | Missed Range

- Prediction: up +1%~+3%
- Actual: up +0.4%
- Main reason: 利多新闻已提前反映在 D 日价格中，次日追涨不足。
- Next adjustment: 遇到已连续上涨超过 3% 的金属，次日看涨区间降一档。
```

偏差原因必须来自 bias_review 的结构化 JSON 输出，Main reason 对应 `bias_reason`，Next adjustment 对应 `next_adjustment`。

## 使用约束

- 跟踪窗口为 1 天。
- 文件名必须以 `_1d.json` 结尾。
- 记录中必须保留当日价、实际价、方向命中、区间命中、误差和偏差原因。
