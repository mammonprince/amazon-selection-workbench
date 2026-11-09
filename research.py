"""Deterministic selection and category scoring."""
import re
from pathlib import Path

import pandas as pd

from peter.metrics import calculate_basic_metrics, calculate_brand_concentration
from peter.risk_checker import detect_risk_keywords
from peter.scoring import calculate_opportunity_score


FIELDS = {
    "asin": ("ASIN", "子ASIN", "子体ASIN"),
    "parent": ("父ASIN", "父体ASIN", "Parent ASIN", "parent_asin"),
    "title": ("商品标题", "标题", "产品名称", "Title"),
    "category": ("商品类目", "类目路径", "类目", "小类目", "Category"),
    "brand": ("品牌", "Brand"),
    "price": ("售价", "价格", "商品价格", "价格($)", "Price"),
    "parent_sales": ("父体月销量", "父体月销", "父体销量", "销量(父)", "销量（父）", "Parent Monthly Sales", "parent_sales"),
    "child_sales": ("子体月销量", "子体月销", "子体销量", "Child Monthly Sales", "child_sales"),
    "rating": ("评分", "商品评分", "Rating"),
    "reviews": ("评论数", "评分数", "Ratings", "Review Count", "reviews"),
    "rank": ("榜单排名", "#", "Rank"),
    "fulfillment": ("配送方式", "履约方式", "Fulfillment", "BuyBox类型"),
    "features": ("产品卖点", "亮点", "Bullet Points"),
    "listed": ("上架时间", "上架日期", "Date First Available", "listed"),
}

NEW_RULE = "父体月销 ≥ 200，且上架日期在观察范围内（含边界）。近一年优先。评分 ≥ 4.3 和目标规格售价 > $30 为优先信号。"
IMPROVE_RULE = "畅销榜评分 < 4.3，评论数严格高于样本中位数，按榜单原顺序最多 10 条"


def header_key(value):
    return re.sub(r"[\s_\-]+", "", str(value)).casefold()


def suggested_columns(columns):
    found = {}
    for field, aliases in FIELDS.items():
        keys = {header_key(alias) for alias in aliases}
        matches = [column for column in columns if header_key(column) in keys]
        found[field] = matches[0] if len(matches) == 1 else None
    if found["category"] is None and "类目路径" in columns:
        found["category"] = "类目路径"
    if found["fulfillment"] is None and "配送方式" in columns:
        found["fulfillment"] = "配送方式"
    seller_sprite_headers = {"ASIN", "父ASIN", "子体销量", "价格($)", "月销量"}
    explicit_sales = {header_key(alias) for alias in FIELDS["parent_sales"]}
    if seller_sprite_headers.issubset(columns) and not explicit_sales.intersection(map(header_key, columns)):
        found["parent_sales"] = "月销量"
    return found


def numeric(series):
    cleaned = series.astype("string").str.strip().str.replace(r"[,$＋+\s]", "", regex=True)
    scale = cleaned.str.extract(r"([kKmM万])$", expand=False).str.lower().map(
        {"k": 1000, "m": 1_000_000, "万": 10_000}
    ).fillna(1)
    return pd.to_numeric(cleaned.str.replace(r"[kKmM万]$", "", regex=True), errors="coerce") * scale


def normalize(raw, mapping):
    selected = [column for column in mapping.values() if column is not None]
    if len(selected) != len(set(selected)):
        raise ValueError("同一列只能映射到一个字段。")
    if not mapping.get("asin"):
        raise ValueError("请先选择 ASIN 对应的列。")
    data = pd.DataFrame({field: raw[mapping[field]] if mapping.get(field) is not None else pd.NA
                         for field in FIELDS})
    for name in ("price", "parent_sales", "child_sales", "rating", "reviews"):
        data[name] = numeric(data[name])
        data[name] = data[name].where(data[name].ge(0) & data[name].lt(float("inf")))
    data["price"] = data["price"].where(data["price"].gt(0))
    data["rating"] = data["rating"].where(data["rating"].between(1, 5))
    data["listed"] = pd.to_datetime(data["listed"].astype("string"), format="mixed", errors="coerce").dt.normalize()
    for field in ("asin", "parent", "title", "category", "brand", "fulfillment", "features"):
        data[field] = data[field].astype("string").str.strip().replace("", pd.NA)
    data["asin"] = data["asin"].str.upper()
    data["parent"] = data["parent"].str.upper()
    data = data.dropna(subset=["asin"]).drop_duplicates("asin")
    # A parent-level value may appear on several child rows. Reuse it only when unambiguous.
    known = data.dropna(subset=["parent", "parent_sales"]).groupby("parent")["parent_sales"]
    consistent = known.first().where(known.nunique().eq(1))
    conflicts = known.nunique().loc[lambda values: values.gt(1)].index
    data["sales_conflict"] = data["parent"].isin(conflicts)
    data["parent_sales"] = data["parent_sales"].fillna(data["parent"].map(consistent))
    data.loc[data["sales_conflict"], "parent_sales"] = pd.NA
    return data


def exclusion_terms(text):
    return list(dict.fromkeys(term.strip().casefold() for term in re.split(r"[,，;；\n]+", text) if term.strip()))


def exclude(data, text):
    terms = exclusion_terms(text)
    if not terms:
        return data.copy(), data.iloc[:0].assign(exclusion="")
    searchable = data["category"].fillna("") + " / " + data["title"].fillna("")
    def matching(value):
        return "、".join(term for term in terms if re.search(
            r"(?<!\w)" + re.escape(term) + r"(?!\w)" if term.isascii() else re.escape(term),
            value.casefold(),
        ))
    tagged = data.assign(exclusion=searchable.map(matching))
    matched = tagged["exclusion"].ne("")
    return data.loc[~matched].copy(), tagged.loc[matched].copy()


def board_union(best, new):
    """Keep the best-seller snapshot when both boards contain the same ASIN."""
    best_asins, new_asins = set(best["asin"]), set(new["asin"])
    combined = pd.concat([best, new], ignore_index=True).drop_duplicates("asin").copy()
    combined["source"] = combined["asin"].map(
        lambda asin: "畅销榜 / 新品榜" if asin in best_asins and asin in new_asins
        else "畅销榜" if asin in best_asins else "新品榜"
    )
    conflicts = {}
    for row in best.merge(new, on="asin", suffixes=("_best", "_new")).to_dict("records"):
        asin = row["asin"]
        match = combined["asin"].eq(asin)
        comparable = ("parent", "parent_sales", "listed", "price", "rating")
        different = [field for field in comparable
                     if pd.notna(row[f"{field}_best"]) and pd.notna(row[f"{field}_new"])
                     and row[f"{field}_best"] != row[f"{field}_new"]]
        notes = ["两榜不一致：" + "、".join(FIELDS[field][0] for field in different)] if different else []
        if set(different) & {"parent", "parent_sales", "listed"}:
            combined.loc[match, "parent_sales"] = pd.NA
        else:
            filled = []
            for field in ("parent", "parent_sales", "listed", "fulfillment"):
                if field == "parent_sales" and (row["sales_conflict_best"] or row["sales_conflict_new"]):
                    continue
                if pd.isna(row[f"{field}_best"]) and pd.notna(row[f"{field}_new"]):
                    combined.loc[match, field] = row[f"{field}_new"]
                    filled.append(FIELDS[field][0])
            if filled:
                notes.append("新品榜补足：" + "、".join(filled))
        if notes:
            conflicts[asin] = "；".join(notes)
    combined["source_note"] = combined["asin"].map(conflicts).fillna("")
    return combined


def new_product_checks(data, as_of, years):
    as_of = pd.Timestamp(as_of).normalize()
    return pd.DataFrame({
        "sales": data["parent_sales"].ge(200).fillna(False),
        "date": data["listed"].between(as_of - pd.DateOffset(years=years), as_of),
        "fba": data["fulfillment"].str.fullmatch(
            r"(?:FBA|AMZ)(?:\s*[（(]亚马逊配送[）)])?|Fulfilled by Amazon|亚马逊配送",
            case=False, na=False,
        ),
    }, index=data.index)


def product_paths(data, as_of, baseline=None, years=2, fba_only=False):
    as_of = pd.Timestamp(as_of).normalize()
    baseline = data if baseline is None else baseline
    reviews = baseline["reviews"].dropna()
    median = float(reviews.median()) if not reviews.empty else None
    checks = new_product_checks(data, as_of, years)
    valid = checks["sales"] & checks["date"]
    if fba_only:
        valid &= checks["fba"]
    candidates = data.assign(
        new_one_year=data["listed"].between(as_of - pd.DateOffset(years=1), as_of),
        target_price=data["price"].gt(30).fillna(False),
        preferred_rating=data["rating"].ge(4.3).fillna(False),
    ).loc[valid.fillna(False)].sort_values(
        ["new_one_year", "target_price", "preferred_rating", "parent_sales", "listed"],
        ascending=False, kind="stable"
    )
    if median is None:
        improvements = baseline.iloc[:0].copy()
    else:
        improve = baseline["rating"].lt(4.3) & baseline["reviews"].gt(median)
        improvements = baseline.loc[improve.fillna(False)].head(10).copy()
    missing = data[["parent_sales", "listed"]].isna().any(axis=1) | data["listed"].gt(as_of)
    review_needed = data.loc[missing].copy()
    review_needed["data_issue"] = [
        "；".join(filter(None, [
            "父体月销缺失或冲突" if pd.isna(row.parent_sales) else "",
            "上架日期缺失或晚于观察日" if pd.isna(row.listed) or row.listed > as_of else "",
        ]))
        for row in review_needed.itertuples()
    ]
    return candidates, improvements, review_needed, median


def category_score(data):
    frame = data.rename(columns={"reviews": "review_count"})
    # The upstream score counts available fields; omit fully missing fields
    # introduced by our optional mapping rather than claiming they were supplied.
    frame = frame.dropna(axis=1, how="all")
    metrics = calculate_basic_metrics(frame)
    risk = detect_risk_keywords(frame)
    result = calculate_opportunity_score(
        frame, metrics, risk, str(Path(__file__).parent / "peter" / "scoring_rules.yaml")
    )
    dimensions = [
        ("market_demand", "市场需求", 20, "累计评论严格超过 10万 / 5万 / 2万 / 5千，依次得 20 / 16 / 12 / 8 分，其余 4 分。"),
        ("competition_intensity", "竞争强度", 15, "Top 品牌占比 <10% / <20% / >40% 时取满分 90% / 70% / 30%，其余 60%；均分 >4.5 再乘 0.7。"),
        ("price_opportunity", "价格机会", 15, "覆盖 ≥4 / ≥2 / 其余价格带，取满分 80% / 60% / 50%。"),
        ("improvement_opportunity", "改良机会", 20, "低评分且高评论商品占比严格超过 30% / 15% / 5%，取满分 90% / 60% / 40%，其余 30%。"),
        ("brand_concentration", "品牌集中度", 10, "Top 品牌占比 >30% / >15% / 其余，取满分 30% / 50% / 80%。"),
        ("risk_level", "风险等级", 10, "按参考项目的风险词检测，低 / 中 / 高风险得 10 / 5 / 1 分。"),
        ("data_quality", "数据质量", 10, "标题、售价、评分、评论数、品牌、排名六列中，按可用列占比给分。"),
    ]
    details = pd.DataFrame([
        {"维度": label, "满分": weight, "得分": result["dimension_scores"][key]["score"],
         "评分依据": result["dimension_scores"][key]["reason"], "规则": rule}
        for key, label, weight, rule in dimensions
    ])
    return result, details, metrics, risk, calculate_brand_concentration(frame)


def sample_data(as_of):
    records = [
        ("A", "Tool shelf bracket 2-pack", "Tools", "Oak", 34.99, 320, 45, 4.5, 58, 190),
        ("A", "Tool shelf bracket 4-pack", "Tools", "Oak", 46.99, 320, 275, 4.5, 58, 190),
        ("B", "Garage seal reinforced", "Tools", "Elm", 32.99, 200, 124, 4.3, 95, 310),
        ("C", "Garage seal basic", "Tools", "Elm", 24.00, 610, 400, 4.2, 920, 890),
        ("D", "Floor protection film standard", "Tools", "Birch", 29.95, 470, 210, 4.1, 640, 570),
        ("E", "Floor protection film heavy", "Tools", "Spruce", 39.99, 380, 110, 4.6, 37, 485),
        ("F", "Garage guide roller", "Tools", "Maple", 27.99, 199, 160, 4.4, 155, 108),
        ("G", "Cabinet protection tape", "Tools", "Pine", 33.99, None, 260, 4.5, 42, 200),
        ("H", "Children's toy bracket kit", "儿童玩具", "Oak", 37.99, 520, 450, 4.7, 85, 50),
        ("I", "Adjustable work belt", "服装", "Birch", 31.99, 420, 245, 4.6, 75, 160),
        ("J", "Heavy duty garage seal", "Tools", "Willow", 42.99, 295, 117, 4.4, 43, 850),
    ]
    rows = [
        (f"DEMO{index:06d}", f"DEMOP{parent}", title, cat, brand, price, parent_sales,
         child_sales, rating, reviews, as_of - pd.Timedelta(days=days))
        for index, (parent, title, cat, brand, price, parent_sales, child_sales, rating, reviews, days) in enumerate(records, 1)
    ]
    return pd.DataFrame(rows, columns=["ASIN", "父体ASIN", "商品标题", "类目", "品牌", "售价",
                                      "父体月销量", "子体月销量", "评分", "评论数", "上架日期"])
