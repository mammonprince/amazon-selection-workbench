import json
from hashlib import sha256
from zipfile import BadZipFile

import pandas as pd
import streamlit as st

from directions import add_directions, direction_summary, enrich_products
from exclusions import classify_products
from history import annotate_history, build_history, history_points, sample_history
from reporting import asin_url, direction_label, link_asins, offline_report, reference_sections
from providers import PROVIDERS, configuration, generate_brief
from research import FIELDS, IMPROVE_RULE, NEW_RULE, category_score, exclude, normalize
from research import board_union, new_product_checks, product_paths, sample_data, suggested_columns


LABELS = {key: aliases[0] for key, aliases in FIELDS.items()}
LABELS.update({
    "new_one_year": "近一年上架",
    "source": "榜单来源",
    "source_note": "来源差异",
    "target_price": "当前价 > $30",
    "preferred_rating": "评分 ≥ 4.3",
    "season": "季节性",
    "price_alert": "价格异动",
    "tradeoff": "降价同期走量",
    "history_coverage": "历史覆盖",
    "direction": "产品方向",
    "amazon_terms": "亚马逊英文搜索词",
    "sourcing_terms": "1688 中文找货词",
    "term_source": "关键词来源",
    "exclusion": "排除原因",
    "data_issue": "待核原因",
})
DISPLAY = ["asin", "parent", "title", "brand", "price", "parent_sales", "child_sales", "rating", "reviews", "listed", "fulfillment"]


def product_results(data, extras):
    result = data[[*DISPLAY, *extras]].rename(columns=LABELS).copy()
    result["上架时间"] = result["上架时间"].dt.strftime("%Y-%m-%d")
    return result


def download_products(result, key, label="下载结果 CSV"):
    export = result.copy()
    for col in export.select_dtypes(include=["object", "string"]):
        export[col] = export[col].map(
            lambda value: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
        )
    st.download_button(label, export.to_csv(index=False).encode("utf-8-sig"),
                       f"{key}.csv", "text/csv", key=key)


def show_products(data, extras=(), key=None, hide_terms=False, download_label="下载结果 CSV"):
    result = product_results(data, extras)
    shown = result.copy()
    if hide_terms:
        shown = shown.drop(columns=[LABELS["amazon_terms"], LABELS["sourcing_terms"]], errors="ignore")
    if "产品方向" in shown:
        shown["产品方向"] = shown["产品方向"].map(direction_label)
    links = {}
    for column, demo_label in (("ASIN", "模拟编号"), ("父ASIN", "模拟父编号")):
        urls = shown[column].map(asin_url)
        if urls.notna().any():
            shown[column] = urls
            links[column] = st.column_config.LinkColumn(column, display_text=r"/dp/([A-Z0-9]{10})$")
        elif shown[column].dropna().str.startswith("DEMO").any():
            shown = shown.rename(columns={column: demo_label})
    st.dataframe(shown, hide_index=True, column_config=links)
    if key and len(result):
        download_products(result, key, download_label)


def show_search_terms(amazon, sourcing):
    columns = st.columns(2)
    for column, field, value in zip(columns, ("amazon_terms", "sourcing_terms"), (amazon, sourcing)):
        column.markdown(f"**{LABELS[field]}**")
        if value.startswith("待核"):
            column.caption(value)
        else:
            column.code(value, language=None)


def read_board(upload, label, key):
    if upload.name.lower().endswith(".xlsx"):
        with pd.ExcelFile(upload, engine="openpyxl") as workbook:
            default = workbook.sheet_names.index("US") if "US" in workbook.sheet_names else 0
            sheet = st.sidebar.selectbox(label + "工作表", workbook.sheet_names, index=default, key=key + "_sheet")
            raw = workbook.parse(sheet)
    else:
        try:
            raw = pd.read_csv(upload, encoding="utf-8-sig")
        except UnicodeDecodeError:
            upload.seek(0)
            raw = pd.read_csv(upload, encoding="gb18030")
    with st.expander(label + " · 检查表头映射", expanded=False):
        st.caption("标准卖家精灵表的「月销量」映射到父体月销量。两列同时存在时，配送优先读取「配送方式」。可手动调整。")
        options = [None, *raw.columns]
        suggested = suggested_columns(raw.columns)
        cols = st.columns(3)
        mapping = {
            field: cols[index % 3].selectbox(
                LABELS[field], options, index=options.index(suggested[field]),
                format_func=lambda value: "未映射" if value is None else str(value), key=f"{key}_{field}"
            )
            for index, field in enumerate(FIELDS)
        }
        for field in ("parent_sales", "listed"):
            if mapping[field] is None:
                st.warning(f"{LABELS[field]}未映射，该表的对应商品会进入新品字段待核。")
    return normalize(raw, mapping)


st.set_page_config(page_title="亚马逊 AI 选品工作台", layout="wide")
st.title("亚马逊 AI 选品工作台")
st.caption("v0.4.6 · Amazon US · 双榜分析、历史复核与选品报告")
with st.sidebar:
    st.header("1 · 数据与规则")
    as_of = st.date_input("数据观察日", help="使用榜单导出的统计日期；历史只读观察月之前的完整月份。")
    uploaded = st.file_uploader("畅销榜 · 普通导出", type=["xlsx", "csv"])
    best_history = st.file_uploader("畅销榜 · 导出明细（可选）", type=["xlsx"])
    new_upload = st.file_uploader("新品榜 · 普通导出（可选）", type=["xlsx", "csv"])
    new_history = st.file_uploader("新品榜 · 导出明细（可选）", type=["xlsx"])
    st.caption("请用同一站点、同一小类目的榜单；没有新品榜时留空。")
    years = st.radio("新品上架范围", [2, 1], format_func=lambda value: f"近 {value} 年")
    fba_only = st.checkbox("新品只看明确 FBA / 亚马逊配送")
    policy = st.text_area(
        "用一句话描述你不做的商品",
        placeholder="例如：不做儿童玩具和服装，但成人手作工具和五金配件可以留。",
        max_chars=2000,
    )
    provider = st.selectbox(
        "AI 模型供应商", list(PROVIDERS),
        format_func=lambda name: "OpenAI / ChatGPT" if name == "OpenAI" else name,
    )
    env_key, default_model = configuration(provider)
    typed_key = st.text_input(
        "API 密钥", type="password", key=f"api_key_{provider}",
        placeholder="粘贴所选供应商的 API Key，然后按 Enter",
    ).strip()
    with st.expander("模型设置（可选）"):
        model = st.text_input("模型名称", value=default_model, key=f"model_{provider}").strip() or default_model
    credentials = (typed_key or env_key, model)
    ready = bool(credentials[0])
    if ready:
        st.caption(f"已填写配置 · {model}。点击 AI 功能时才会调用。")
    else:
        st.caption("填入 API 密钥并按 Enter，即可启用 AI 功能。")

if uploaded is None and any((new_upload, best_history, new_history)):
    st.info("先上传畅销榜普通导出，建立类目分析的基线。")
    st.stop()
try:
    if uploaded is None:
        raw = sample_data(pd.Timestamp(as_of))
        best = normalize(raw, suggested_columns(raw.columns))
        st.info("当前为模拟数据，DEMO 是模拟编号。上传真实商品表后，点击 ASIN 即可打开 Amazon US 商品页。")
    else:
        best = read_board(uploaded, "畅销榜", "best")
    new = read_board(new_upload, "新品榜", "new") if new_upload is not None else best.iloc[:0].copy()
except (ValueError, OSError, ImportError, BadZipFile) as error:
    st.error(f"读取榜单失败：{error}")
    st.stop()
if best.empty:
    st.warning("畅销榜没有可用 ASIN，请检查普通导出的工作表和字段映射。")
    st.stop()
normalized = board_union(best, new)
points = sample_history(as_of) if uploaded is None else []
for filename, file in (("畅销榜明细", best_history), ("新品榜明细", new_history)):
    if file is None:
        continue
    try:
        extracted = history_points(file, as_of)
        points.extend(extracted)
        if not extracted:
            st.warning(f"{filename}没有可用的完整月份数据，请检查工作表与观察日。")
    except (ValueError, OSError, ImportError, BadZipFile) as error:
        st.warning(f"{filename}读取失败：{error}。其他数据可以继续分析。")
history = build_history(points)

data, excluded = exclude(normalized, "")
if not policy.strip() and uploaded is None:
    use_sample_exclusions = st.sidebar.checkbox("演示：排除服装和儿童玩具", value=True)
    if use_sample_exclusions:
        data, excluded = exclude(normalized, "服装\n儿童玩具")
elif policy.strip():
    evidence = normalized[["asin", "title", "category"]].fillna("").to_dict("records")
    fingerprint = sha256(json.dumps([policy.strip(), provider, model, evidence], ensure_ascii=False).encode()).hexdigest()
    st.sidebar.caption("点击分析会向所选供应商发送排除要求、ASIN、标题和类目。")
    if not ready:
        st.sidebar.info("在左侧填入 API 密钥后可运行 AI 排除。")
    if st.sidebar.button("让 AI 分析排除要求", disabled=not ready):
        st.session_state.pop("ai_exclusion", None)
        st.session_state.pop(f"apply_ai_{fingerprint}", None)
        try:
            with st.spinner("AI 正在检查商品与例外条件…"):
                decisions = classify_products(provider, policy.strip(), evidence, credentials=credentials)
            st.session_state.ai_exclusion = (fingerprint, decisions)
        except (RuntimeError, ValueError) as error:
            st.error(str(error))
    saved = st.session_state.get("ai_exclusion")
    if saved and saved[0] == fingerprint:
        tagged = normalized.assign(
            decision=normalized["asin"].map(lambda asin: saved[1][asin][0]),
            exclusion=normalized["asin"].map(lambda asin: saved[1][asin][1]),
        )
        proposed = tagged.loc[tagged["decision"].eq("exclude")]
        pending = tagged.loc[tagged["decision"].eq("review")]
        with st.expander(f"AI 排除预览 · 建议排除 {len(proposed)} 条，待核 {len(pending)} 条", expanded=True):
            if not proposed.empty:
                st.write("建议排除")
                show_products(proposed, ["exclusion"])
            if not pending.empty:
                st.write("待核 · 暂留在样本中")
                show_products(pending, ["exclusion"])
            if st.checkbox("应用这一轮 AI 排除结果", key=f"apply_ai_{fingerprint}"):
                excluded = proposed
                data = tagged.loc[tagged["decision"].ne("exclude")].copy()
    elif ready:
        st.info("这段排除要求尚未应用。可以先运行 AI 分析，再查看预览。")
summary = st.columns(3)
summary[0].metric("上传样本 · 去重 ASIN", len(normalized))
summary[1].metric("命中排除规则", len(excluded))
summary[2].metric("进入分析", len(data))
if len(excluded):
    with st.expander("查看排除明细"):
        show_products(excluded, ["exclusion"])
if data.empty:
    st.warning("当前规则排除了全部记录。调整左侧排除词即可重新计算。")
    st.stop()

baseline = best.loc[best["asin"].isin(data["asin"])].assign(source="畅销榜")
candidates, improvements, review_needed, median = product_paths(data, as_of, baseline, years, fba_only)
candidates = annotate_history(candidates, history)
improvements = annotate_history(improvements, history)
scoring, details, metrics, risk, brands = category_score(baseline)
score = scoring if not baseline.empty else None
group_evidence = data[["asin", "title", "category", "features"]].fillna("").to_dict("records")
group_key = sha256(json.dumps([provider, model, group_evidence], ensure_ascii=False).encode()).hexdigest()
with st.sidebar:
    st.caption("AI 分组会发送保留样本的 ASIN、标题、类目和卖点。")
    if st.button("用 AI 分组并补齐搜索词", disabled=not ready):
        try:
            with st.spinner("正在识别同类方向和双语搜索词…"):
                st.session_state.direction_result = (group_key, enrich_products(provider, group_evidence, credentials=credentials))
        except (RuntimeError, ValueError) as error:
            st.error(str(error))
    if not ready:
        st.caption("在左侧填入 API 密钥后可生成 AI 分组与搜索词。")
saved_group = st.session_state.get("direction_result")
ai = saved_group[1] if saved_group and saved_group[0] == group_key else {}
grouped = add_directions(data, ai)
candidates = add_directions(candidates, ai)
improvements = add_directions(improvements, ai)
display_candidates = candidates.loc[~candidates["direction"].str.startswith("待细分 · ", na=False)]
directions = direction_summary(display_candidates, grouped, as_of)
market_tab, new_tab, improve_tab, ai_tab = st.tabs(["类目评分", "新品候选", "改良品候选", "AI 调研摘要"])
history_cols = ["season", "price_alert", "tradeoff", "history_coverage"]
keyword_cols = ["direction", "amazon_terms", "sourcing_terms", "term_source"]
allowed = set(normalized["asin"]) | set(normalized["parent"].dropna())

with market_tab:
    st.header("2 · 类目评分")
    st.metric("类目得分", f"{score['total_score']} / 100 · {score['level']}" if score else "数据不足")
    if score:
        st.dataframe(details, hide_index=True)
        st.write("风险命中词：" + ("、".join(risk["matched_keywords"]) or "无"))
    dated = data.loc[data["listed"].le(pd.Timestamp(as_of))]
    recent = dated["listed"].ge(pd.Timestamp(as_of) - pd.DateOffset(years=1))
    signals = st.columns(2)
    signals[0].metric("两榜去重后近一年上架", f"{recent.sum()} / {len(dated)}" if len(dated) else "数据不足")
    signals[1].metric("近一年且达到新品门槛", int(candidates["new_one_year"].sum()))

with new_tab:
    st.header("3 · 新品候选")
    st.write(NEW_RULE)
    st.caption("售价和评分用于优先排序。父体月销低于 200 不进入本清单。历史月销量与当前父体月销分别使用。")
    st.metric("已归入产品方向的候选 ASIN", len(display_candidates))
    if fba_only:
        checks = new_product_checks(data, as_of, years)
        before_fba = checks["sales"] & checks["date"]
        blocked = before_fba & ~checks["fba"]
        if blocked.any():
            missing_fba = (blocked & data["fulfillment"].isna()).sum()
            st.info(f"{int(blocked.sum())} 条已满足销量和时间条件，但未通过 FBA 筛选，其中 {missing_fba} 条未读到配送方式。")
    with st.expander("查一个 ASIN 为什么没入选"):
        query = st.text_input("输入 ASIN", placeholder="例如 B0H5QFK292").strip().upper()
        if query:
            item = data.loc[data["asin"].eq(query)]
            if item.empty:
                if excluded["asin"].eq(query).any():
                    st.warning("此商品命中了已应用的排除规则。")
                    show_products(excluded.loc[excluded["asin"].eq(query)], ["exclusion"])
                else:
                    st.warning("上传的两张榜单中没有读到这个 ASIN。请核对导出表是否包含它。")
            else:
                check = new_product_checks(item, as_of, years).iloc[0]
                row = item.iloc[0]
                reasons = []
                if not check["sales"]:
                    reasons.append("父体月销量缺失或冲突" if pd.isna(row["parent_sales"]) else "父体月销量低于 200")
                if not check["date"]:
                    reasons.append("上架日期缺失或无法解析" if pd.isna(row["listed"])
                                   else "上架日期晚于观察日" if row["listed"] > pd.Timestamp(as_of)
                                   else f"上架日期早于观察日往前 {years} 年")
                if fba_only and not check["fba"]:
                    reasons.append("配送方式未读取" if pd.isna(row["fulfillment"]) else "配送方式未明确标为 FBA")
                if reasons:
                    st.warning("未入选：" + "；".join(reasons))
                elif not display_candidates["asin"].eq(query).any():
                    st.info("已通过新品筛选，但尚未归入明确的产品方向。可在左侧用 AI 分组。")
                else:
                    st.success("已通过当前新品筛选条件。")
                show_products(item, ["source", "source_note"])
    candidate_cols = ["source", "source_note", "new_one_year", "target_price", "preferred_rating",
                      *keyword_cols, *history_cols]
    for number, direction in enumerate(directions, start=1):
        items = display_candidates.loc[display_candidates["direction"].eq(direction["direction"])]
        with st.expander(f"{direction['grade']} · {direction_label(direction['direction'])}"):
            st.markdown(f"**本方向入选商品 · {len(items)} 条**")
            show_products(items, candidate_cols, key=f"new-candidates-{number}", hide_terms=True,
                          download_label="下载本方向 CSV")
            st.write(direction["reason"], direction["price_state"])
            evidence = [*direction["evidence_asins"], *direction["price_asins"]]
            label = "参考 ASIN" if any(asin_url(asin) for asin in evidence) else "参考模拟编号"
            st.markdown(link_asins(f"{label} " + "、".join(evidence), allowed))
            show_search_terms(direction["amazon_terms"], direction["sourcing_terms"])
    if directions:
        download_products(product_results(display_candidates, candidate_cols), "new-candidates", "下载全部新品候选 CSV")
    elif not candidates.empty:
        st.info("已有商品达到新品门槛。可在左侧点击「用 AI 分组并补齐搜索词」，识别产品方向后查看。")
    else:
        st.info("当前样本没有满足新品门槛的商品。")
    if len(review_needed):
        with st.expander(f"字段待核 · {len(review_needed)} 条"):
            show_products(review_needed, ["source", "source_note", "data_issue"])

with improve_tab:
    st.header("4 · 改良品候选")
    st.write(IMPROVE_RULE)
    st.metric("畅销榜评论数中位数", f"{median:g}" if median is not None else "数据不足")
    st.caption("评论数和评分提供后续调研线索。具体改良点需要真实评论支持。")
    if not improvements.empty:
        st.subheader("逐条搜索词")
        st.caption("鼠标移到词框右侧，点复制图标即可复制该组关键词。")
        for row in improvements.itertuples():
            with st.container(border=True):
                st.markdown(link_asins(f"商品 {row.asin}", allowed))
                if pd.notna(row.title):
                    st.text(str(row.title))
                show_search_terms(row.amazon_terms, row.sourcing_terms)
    show_products(improvements, ["source", *keyword_cols, *history_cols], key="improvement-candidates", hide_terms=True)

with ai_tab:
    st.header("5 · AI 调研摘要")
    evidence_fields = ["asin", "parent", "title", "price", "parent_sales", "child_sales", "rating", "reviews",
                       "listed", "source", *keyword_cols, *history_cols]
    evidence = {
        "观察日": str(as_of), "样本": f"畅销榜上传 {len(best)} 条；新品榜上传 {len(new)} 条；去重并排除后 {len(data)} 条。",
        "评分": score, "指标": metrics, "风险": risk, "品牌": brands, "产品方向": directions,
        "畅销榜商品": json.loads(baseline[["asin", "title", "features", "price", "rating", "reviews"]]
                            .to_json(orient="records")),
        "历史数据说明": "季节性与价格异动使用观察月之前的完整月份。",
        
        "新品候选": json.loads(display_candidates[[*evidence_fields, "new_one_year"]].to_json(orient="records", date_format="iso")),
        "改良品候选": json.loads(improvements[evidence_fields].to_json(orient="records", date_format="iso")),
    }
    report_key = sha256(json.dumps([provider, model, evidence], ensure_ascii=False).encode()).hexdigest()
    st.caption("生成 AI 版会发送畅销榜必要字段、评分、候选及历史预警。")
    if not ready:
        st.info("在左侧选择供应商，填入 API 密钥并按 Enter，即可生成 AI 调研报告。")
    if st.button("生成 AI 调研报告", disabled=not ready):
        try:
            with st.spinner("正在生成调研报告…"):
                st.session_state.report = (report_key, generate_brief(provider, evidence, credentials=credentials))
        except (RuntimeError, ValueError) as error:
            st.error(str(error))
    saved_report = st.session_state.get("report")
    use_ai = saved_report and saved_report[0] == report_key
    st.caption("AI 报告 · 已生成" if use_ai else "本地模板报告 · 可在配置模型后生成 AI 版")
    report = link_asins(saved_report[1] if use_ai else offline_report(evidence), allowed)
    report += "\n\n" + reference_sections(evidence, allowed)
    st.markdown(report)
    st.download_button("下载 Markdown 报告", report, "selection-report.md", "text/markdown")
