---
name: daily-metal-briefing
description: Generate daily or weekly structured metal market briefings for cutting tool raw materials. Uses current spot prices, multi-source validation, supply-chain/news scanning, and next-day volatility review.
---

# 每日金属行情早报

自动生成刀具原材料（钴、钨、镍、铁矿石，以及按需的铜/铝/锡/锌/铅）每日/每周结构化行情简报。

所有方向判断基于当日现货价、新闻/供应链事件、国际价格信号、需求和季节性信息。

## 数据源

- `spot-price-fetcher`：获取当日现货价。
- `multi-source-validator`：对 SMM、长江有色、生意社等报价做交叉验证。
- `news-impact-mapper`：把新闻事件映射为涨跌方向和影响强度。
- `supply-chain-scanner`：扫描矿山、出口管制、配额、物流和供应端事件。
- `global-price-signal-detector`、`price-driver-matrix`、`demand-sensor`：补充 LME/SHFE、资金面、季节性、下游需求等信息。
- `next-day-volatility-tracker`：登记今日对明日的波动判断，并复盘昨日预测。

## 日度早报流程

1. 获取目标金属当日现货价，并记录报价日期、来源和单位。
2. 对关键金属执行多源验证，标注价格差异和可信度。
3. 扫描近 24 小时新闻和供应链事件。
4. 基于现货价 + 新闻/供应链信息，给出 D+1 方向判断和幅度区间。
5. 调用 `next-day-volatility-tracker.today_predict` 写入 `.workbuddy/memory/backtest/predictions/{METAL}_{YYYY-MM-DD}_1d.json`。
6. 调用 `next-day-volatility-tracker.tomorrow_verify` 检查昨日预测，如已有实际价则输出复盘。

## 日度早报格式

```markdown
# 刀具原材料行情早报 [日期]

**今日核心关注：** {最关键的行情变化}

## 当日现货

| 金属 | 现货价 | 单位 | 报价日 | 来源 | 多源一致性 |
|------|--------|------|--------|------|------------|
| CO | {price} | CNY/吨 | {date} | {source} | {高/中/低} |

## 新闻与供应链

| 金属 | 关键事件 | 方向 | 强度 | 时效 | 来源 |
|------|----------|------|------|------|------|
| CO | {event} | ↑/↓/→ | 高/中/低 | {time} | {source} |

## 明日波动判断

| 金属 | 今日价 | D+1 方向 | 幅度区间 | 置信度 | 核心理由 |
|------|--------|----------|----------|--------|----------|
| CO | {price} | 看涨/看跌/震荡 | {如 +1%~+3%} | 高/中/低 | {reason} |

## 昨日复盘 Headline 写作规范

每日早报的 `yesterday_review.headline` 由 LLM 在 verify 阶段完成后自动生成，写入 `.workbuddy/memory/reports/headline_{date}.json`。

**写作要求：**
- 1 句话，15-40 个中文字符
- 必须提及整体方向命中率
- 如有未命中，用通俗语言点出关键原因（不说"供应扰动预期偏差"，说"铜矿罢工影响被高估"）
- 语气：专业但可读，像资深分析师给同事的晨间笔记
- 禁止：空洞措辞（"表现尚可"）、夸张修辞（"惨遭打脸"）、过度技术术语

**好例子：**
- `昨日方向命中率60%，CU和NI被夜盘宏观逆转拖累，其余3个品种均在区间内。`
- `昨日3/5命中方向，CO因非洲物流恢复超预期误判下跌，已记录学习规则。`
- `昨日全部命中，5个品种方向与区间均在预测范围内，供给端信号持续有效。`

## 昨日预测复盘

| 金属 | 昨日预测 | 今日实际 | 实际波动 | 方向命中 | 区间命中 | 偏差原因 |
|------|----------|----------|----------|----------|----------|----------|
| CO | {predicted} | {actual} | {pct} | 是/否 | 是/否 | {reason} |

## 今日策略速览

- **钴：** {一句话策略}
- **钨：** {一句话策略}
- **镍：** {一句话策略}
- **铁矿石：** {一句话策略}

**综合建议：** {一句话概括采购团队今天最该做什么}
```

## 周度/月度简报

周度和月度简报只复盘已登记的 1 日预测记录：

- 汇总每日现货价变化。
- 汇总 `results/*_1d.json` 的方向命中率、区间命中率、平均误差。
- 归纳 `learnings/{METAL}_learnings.md` 中的偏差经验。
- 汇总单日方向命中率、区间命中率和平均误差。

## 与 Team 协作

本 Skill 可由主理人（栾金川）在以下时机调用：

- 用户说“今天行情怎么样” → 日度早报。
- 用户说“这周总结一下” → 周度简报。
- 用户说“预测准不准” → 读取 `next-day-volatility-tracker` 的 1 日复盘结果。
## HTML publishing

For a daily briefing, write report JSON with `report_type: "daily_briefing"`.
For a weekly or monthly briefing, write report JSON with
`report_type: "weekly_briefing"`.

After saving the JSON, run:

```powershell
py scripts\build_html_report.py path\to\report.json
```

Send the generated Markdown links from the script's `Chat links:` section back
to the user. Do not hand-write a one-off WorkBuddy `index.html`; the script
maintains the repository report center and WorkBuddy root/task-folder indexes.
