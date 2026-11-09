"""Evidence-based research report and US Amazon product links."""
import re
from urllib.parse import quote_plus


ASIN = re.compile(r"(?<![A-Za-z0-9])([A-Z0-9]{10})(?![A-Za-z0-9])")
SECTIONS = [
    "基础信息", "类目概况", "市场需求判断", "竞争强度判断", "价格带分析", "品牌集中度分析",
    "潜在产品机会", "可能的差异化方向", "风险点", "综合评分", "是否建议进入产品开发", "下一步验证清单",
]


def asin_url(asin):
    return f"https://www.amazon.com/dp/{asin}" if isinstance(asin, str) and ASIN.fullmatch(asin) and not asin.startswith("DEMO") else None


def direction_label(direction):
    return "待细分" if direction.startswith("待细分 · ") else direction


def link_asins(text, allowed):
    # Normalize model-generated links to plain labels before linking known IDs ourselves.
    text = re.sub(r"\[([^\]\n]+)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"https?://[^\s)>]+", "", text)

    def replace(match):
        asin = match[1].upper()
        url = asin_url(asin)
        return f"[{asin}]({url})" if asin in allowed and url else match[1]

    return re.sub(ASIN.pattern, replace, text, flags=re.IGNORECASE)


def offline_report(evidence):
    m, risk, brands = evidence["指标"], evidence["风险"], evidence["品牌"]
    score = evidence["评分"]
    new, improve = evidence["新品候选"], evidence["改良品候选"]
    directions = evidence["产品方向"]
    alerts = [f"{row['asin']}：{row['season']}；{row['price_alert']}；{row['tradeoff']}"
              for row in [*new, *improve]
              if "旺季" in row["season"] or "疑似" in row["season"] or "%" in row["price_alert"]]
    contents = [
        f"Amazon US · 观察日 {evidence['观察日']}。{evidence['样本']}",
        f"畅销榜有效样本 {m['product_count']} 条。新品候选 {len(new)} 条，改良品候选 {len(improve)} 条。",
        f"畅销榜累计评论数 {m['total_review_count']:,}。评论是需求线索，父体销量估算与子体销量分开。",
        f"平均评分 {m['avg_rating']}。同类组判断见新品候选页，注意品牌与老品壁垒。",
        " · ".join(f"${label}：{count} 条" for label, count in m["price_bands"].items()) or "价格数据待核。",
        f"Top 1 品牌份额 {brands['top_1_brand_share']:.1%}，Top 3 {brands['top_3_brand_share']:.1%}。",
        "\n".join(f"- {direction_label(item['direction'])} · {item['grade']} · {'、'.join(item['evidence_asins'])}"
                  for item in directions) or "当前样本没有达到新品硬门槛的方向。",
        ("待查真实差评与规格缺口：" + "、".join(row["asin"] for row in improve)) if improve else "待复核同类规格、组合与实际采购条件。",
        f"关键词风险：{risk['risk_level']}。命中：{'、'.join(risk['matched_keywords']) or '无'}。"
        + ("\n" + "\n".join(f"- {alert}" for alert in dict.fromkeys(alerts)) if alerts else "\n历史风险详情见候选页。"),
        f"{score['total_score']}/100 · {score['level']}。沿用 Peter 原始评分公式。" if score else "畅销榜评分资料不足。",
        "先验证目标规格售价、变体真实销量、供应链和利润，再决定是否开发。",
        "\n".join(f"- {step}" for step in (
            "复核上架日期和父体销量", "确认热卖子体", "用英文词扩展同类 ASIN", "下载真实差评",
            "核查完整月份价格", "核查跨年旺季", "用中文词搜索 1688 并询价", "复核包装、运费、认证与利润",
        )),
    ]
    return "# 选品分析报告\n\n" + "\n\n".join(
        f"## {index}. {title}\n\n{content}"
        for index, (title, content) in enumerate(zip(SECTIONS, contents), 1)
    )


def reference_sections(evidence, allowed):
    baseline, new, improve = evidence["畅销榜商品"], evidence["新品候选"], evidence["改良品候选"]
    references = []
    seen = set()
    for label, candidates in (
        ("榜单头部商品", baseline), ("近期上架候选", [row for row in new if row["new_one_year"]]),
        ("改良品候选", improve), ("近期销量对标", sorted(new, key=lambda row: row["parent_sales"], reverse=True)),
    ):
        row = next((row for row in candidates if row["asin"] not in seen), None)
        if row:
            seen.add(row["asin"])
            references.append(f"- {label} · {row['asin']} · {row['title'] or '标题待核'}")
    sample = baseline[:10]
    words = {}
    for row in sample:
        for word in set(re.findall(r"[A-Za-z]{4,}", f"{row['title'] or ''} {row['features'] or ''}".casefold())):
            words[word] = words.get(word, 0) + 1
    repeated = sorted((word for word in words if words[word] >= 2), key=lambda word: (-words[word], word))
    terms = list(dict.fromkeys(row["amazon_terms"].split("；")[0] for row in [*new, *improve]
                               if not row["amazon_terms"].startswith("待核")))[:4]
    result = [
        "### 竞品对比线索",
        link_asins("\n".join(references), allowed) if references else "当前资料不足以给出可复核竞品。",
        "### 畅销榜前 10 标题与卖点共性",
        "高频词线索：" + "、".join(repeated[:8]) if repeated else "当前样本未观察到重复词。",
        "### 评论分析缺口",
        "需要实际评论文本才能判断高频痛点、购买动机和改良方向。",
        "### Google Trends 搜索入口",
    ]
    result.extend(f"- [{term.replace('[', '').replace(']', '')}](https://trends.google.com/trends/explore?date=all&geo=US&q={quote_plus(term)})"
                  for term in terms)
    if not terms:
        result.append("当前没有可用英文搜索词。")
    return "\n\n".join(result)
