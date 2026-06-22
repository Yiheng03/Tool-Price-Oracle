---
name: tool-cost-model
description: Calculate cutting tool cost estimates from tool specification, material/process/workpiece factors, current spot prices, and event-driven next-day metal movement assumptions.
---

# 刀具成本建模引擎

将“当日金属现货价 + 新闻/供应链驱动的次日波动判断”转化为刀具成本影响估算。

本 Skill 输出当前报价基准和次日风险影响。

## 前置输入

来自上游 agent 的输入：

- 当日现货价：`spot_price_today`、报价日期、来源和单位。
- 次日波动判断：`predicted_direction`、`predicted_range_pct`、核心理由。
- 供应链事件：影响方向、强度、时间窗口。
- 刀具规格、材质、涂层、行业、工艺、工件材料。

## 刀具材质到金属权重

用于估计金属价格变化对刀具成本的传导：

| 刀具材质 | CO | NI | IRON_ORE | AL | CU | ZN | SN | W |
|----------|----|----|----------|----|----|----|----|---|
| CARBIDE（硬质合金） | 0.10 | 0.05 | 0.02 | 0.01 | 0.00 | — | — | 0.82 |
| HSS（高速钢） | 0.15 | 0.15 | 0.55 | 0.05 | 0.05 | 0.03 | 0.02 | — |
| CERAMIC/CBN/PCD | 0.15 | 0.15 | 0.30 | 0.20 | 0.20 | — | — | — |
| STEEL_HOLDER（刀柄） | — | 0.15 | 0.65 | 0.05 | 0.10 | 0.05 | — | — |

## 计算步骤

### 步骤 1：规格解析

从规格字符串提取刃径、刃长、刃数、公差、柄径、总长，并推断刀型和涂层。

### 步骤 2：匹配情境因子

按行业、工艺、工件材料匹配固定因子：

- `fIndustry`：行业因子。
- `fProcess`：工艺因子。
- `fWorkpiece`：工件材料因子。
- `betaG`：金属行情传导系数。
- `eta`：行业需求敏感度。

### 步骤 3：计算当日成本基准

```
base_cost = specBasePrice × fIndustry × fProcess × fWorkpiece × globalCalibration
```

当日金属现货价用于解释成本压力和次日成本影响。

### 步骤 4：计算次日行情影响

对每个关键金属，把 analyst 给出的次日波动区间转为成本影响：

```
metal_impact_pct = metal_weight × betaG × predicted_change_pct
next_day_market_factor = clamp(1 + Σ metal_impact_pct + event_correction_pct, 0.85, 1.15)
```

- `predicted_change_pct` 取次日区间中点，同时保留上下限。
- `event_correction_pct` 只来自供应链/新闻事件判断。
- 钨必须通过现货价和供应链扫描判断。

### 步骤 5：输出刀具成本影响

```
estimated_today_price = base_cost
estimated_next_day_range = base_cost × next_day_market_factor_range
```

输出关注点：

- 当前报价基准。
- 明日金属波动对刀具成本的方向和大致幅度。
- 最大不确定性来源。
- 是否建议锁价、观望或分批。

## 输出格式

```markdown
## 刀具成本影响估算 [日期]

**规格解析：** {刀型} | 刃径 {mm}mm | 刃长 {mm}mm | {刃数}刃 | {涂层}
**材质/场景：** {材质} | {行业} | {工艺} | {工件材料}

### 当日成本基准

| 项目 | 值 | 说明 |
|------|----|------|
| base_cost | ¥{x} | 规格和情境因子计算 |
| fIndustry | {x} | {行业} |
| fProcess | {x} | {工艺} |
| fWorkpiece | {x} | {工件材料} |

### 次日金属波动传导

| 金属 | 今日现货 | 次日判断 | 幅度区间 | 权重 | 成本影响 |
|------|----------|----------|----------|------|----------|
| CO | {price} | 看涨/看跌/震荡 | {pct} | {weight} | {impact} |

### 次日成本区间

| 口径 | 估算值 |
|------|--------|
| 当前基准价 | ¥{x} |
| 明日影响后低位 | ¥{x} |
| 明日影响后高位 | ¥{x} |

**最大不确定性来源：** {金属/事件}
```

## 原则

- 所有价格判断必须能追溯到当日现货价、新闻、供应链事件或用户提供的报价。
- 输出当前基准、次日风险区间和最大不确定性来源。
