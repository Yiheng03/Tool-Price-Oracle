---
name: spot-price-fetcher
description: Fetch current spot prices for cutting-tool-related metals from public sources and return price, unit, date, and source metadata.
---

# 金属现货价采集器

通过公开数据源获取目标金属的当日或最近交易日现货价，作为 D+1 波动判断、次日核对和刀具成本影响估算的基础。

## 支持金属

| 代码 | 金属 | 常用单位 |
|------|------|----------|
| CO | 钴 | CNY/吨 |
| W | 钨 / 钨粉 / 碳化钨 | CNY/公斤 |
| NI | 镍 | CNY/吨 |
| IRON_ORE | 铁矿石 | CNY/吨 或 USD/吨 |
| CU | 铜 | CNY/吨 |
| AL | 铝 | CNY/吨 |
| ZN | 锌 | CNY/吨 |
| SN | 锡 | CNY/吨 |
| PB | 铅 | CNY/吨 |

## 数据源优先级

1. SMM / 上海有色网。
2. 长江有色。
3. 生意社。
4. 交易所或行业协会公开数据。
5. 可信新闻源中的当日报价。

## 工作流程

### 步骤 1：确定金属和日期

输入金属代码、目标日期和期望单位。若目标日期无交易，取最近可用交易日并标注。

### 步骤 2：搜索报价

记录来源、发布时间、价格、单位和口径。若只有价格区间，取中位值并保留上下限。

### 步骤 3：标准化输出

```json
{
  "metal": "CU",
  "price": 78500,
  "unit": "CNY/ton",
  "price_date": "2026-06-22",
  "source": "SMM",
  "source_url": "...",
  "quote_type": "spot average",
  "confidence": "high"
}
```

### 步骤 4：必要时交叉验证

关键金属或报价差异较大的品种，调用 `multi-source-validator`。

## 输出格式

```markdown
## 当日现货价 [{metal}] [{date}]

| 金属 | 价格 | 单位 | 报价日 | 来源 | 可信度 |
|------|------|------|--------|------|--------|
| CU | 78,500 | CNY/吨 | 2026-06-22 | SMM | 高 |
```

## 使用时机

- D 日登记次日波动判断。
- D+1 核对实际价格。
- 每日行情简报。
- 刀具成本影响估算。
