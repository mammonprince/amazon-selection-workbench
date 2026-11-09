"""Read SellerSprite monthly worksheets and annotate candidate evidence."""
import re
from collections import defaultdict

import pandas as pd

from research import numeric

SHEETS = {
    "产品历史月销量": "parent_units",
    "子体历史月销量": "child_units",
    "产品历史月价格": "price",
}
MONTH = re.compile(r"^((?:19|20)\d{2})[-/](0?[1-9]|1[0-2])(?:\s*\([^)]*\))?$")


def history_points(file, as_of):
    cutoff = pd.Timestamp(as_of).to_period("M")
    found = []
    with pd.ExcelFile(file, engine="openpyxl") as book:
        for sheet, metric in SHEETS.items():
            if sheet not in book.sheet_names:
                continue
            frame = book.parse(sheet)
            frame.columns = frame.columns.map(lambda value: str(value).strip())
            if "ASIN" not in frame:
                continue
            identifiers = frame["ASIN"].astype("string").str.strip().str.upper()
            if metric == "parent_units" and "父ASIN" in frame:
                parents = frame["父ASIN"].astype("string").str.strip().str.upper()
                identifiers = parents.mask(parents.isin(["", "-", "--", "N/A"])).fillna(identifiers)
            for column in frame.columns:
                month = MONTH.fullmatch(column)
                if not month:
                    continue
                period = pd.Period(year=int(month[1]), month=int(month[2]), freq="M")
                if period >= cutoff:
                    continue
                values = numeric(frame[column])
                for asin, value in zip(identifiers, values):
                    if pd.notna(asin) and pd.notna(value) and 0 <= value < float("inf") and (metric != "price" or value > 0):
                        found.append((metric, asin, period, float(value)))
    return found


def build_history(points):
    values = defaultdict(set)
    for metric, asin, month, value in points:
        values[metric, asin, month].add(value)
    series = defaultdict(dict)
    conflicts = defaultdict(int)
    for (metric, asin, month), samples in values.items():
        if len(samples) == 1:
            series[metric, asin][month] = next(iter(samples))
        else:
            conflicts[metric, asin] += 1
    return series, conflicts


def season_signal(units):
    if len(units) < 12:
        return "待核：不足 12 个有效月"
    windows = {}
    for start in sorted(units):
        year = [units.get(start + i) for i in range(12)]
        if None not in year and sum(year):
            windows[start] = [sum(year[i:i + 3]) / sum(year) for i in range(10)]
    if not windows:
        return "待核：缺连续 12 个有销量的完整月"
    for start in reversed(windows):
        earlier = windows.get(start - 12)
        if earlier:
            for offset, (prior, recent) in enumerate(zip(earlier, windows[start])):
                if prior >= .5 and recent >= .5:
                    month = start + offset
                    return (f"重复旺季信号：{month - 12}–{month - 10} 占历史12个月 {prior:.0%}；"
                            f"{month}–{month + 2} 占历史12个月 {recent:.0%}")
    share, month = max((share, start + offset) for start, shares in windows.items()
                       for offset, share in enumerate(shares))
    if share >= .5:
        return f"疑似季节性：{month}–{month + 2} 占历史12个月 {share:.0%}，待跨年核验"
    return "未触发季节性预警"


def history_note(row, history):
    series, conflicts = history
    parent = row.asin if pd.isna(row.parent) or row.parent in {"", "-", "--", "N/A"} else row.parent
    prices = series.get(("price", row.asin), {})
    child = series.get(("child_units", row.asin), {})
    parent_units = series.get(("parent_units", parent), {})
    units, level = (child, "子体") if len(child) >= 12 or not parent_units else (parent_units, "父体")
    season = f"{level} · {season_signal(units)}" if units else "待核：无匹配历史销量"
    comparisons = []
    for month, value in sorted(prices.items()):
        previous = prices.get(month - 1)
        if previous is not None:
            comparisons.append((month, previous, value, value / previous - 1))
    changes = [item for item in comparisons if abs(item[3]) >= .25 - 1e-10]
    changes.sort(key=lambda item: (-abs(item[3]), -item[0].ordinal))
    movement = "；".join(f"{month - 1} → {month}：${previous:.2f} → ${value:.2f}（{rate:+.0%}）"
                          for month, previous, value, rate in changes[:3])
    if not movement:
        movement = "未触发 25% 月度预警" if comparisons else "待核：无连续完整月价格"
    tradeoff = []
    for month, _, _, rate in changes:
        previous, current = child.get(month - 1), child.get(month)
        if rate <= -.25 + 1e-10 and previous is not None and current is not None and current > previous:
            tradeoff.append(f"{month}：子体销量 {previous:g} → {current:g}，降价同期走量，原因待核")
    conflict_count = sum(conflicts.get(key, 0) for key in (
        ("price", row.asin), ("child_units", row.asin), ("parent_units", parent)
    ))
    return {
        "season": season, "price_alert": movement, "tradeoff": "；".join(tradeoff[:3]) or "—",
        "history_coverage": f"子体销量 {len(child)} 月 · 父体销量 {len(parent_units)} 月 · 价格 {len(prices)} 月"
                            + (f" · 冲突 {conflict_count} 月值已跳过" if conflict_count else ""),
    }


def annotate_history(frame, history):
    columns = ["season", "price_alert", "tradeoff", "history_coverage"]
    evidence = pd.DataFrame([history_note(row, history) for row in frame.itertuples()],
                            columns=columns, index=frame.index)
    return frame.join(evidence)


def sample_history(as_of):
    cutoff = pd.Timestamp(as_of).to_period("M")
    points = []
    for i in range(24, 0, -1):
        month = cutoff - i
        points += [
            ("child_units", "DEMO000003", month, 200 if month.month in (12, 1, 2) else 35),
            ("price", "DEMO000003", month, 35 if i >= 4 else 24),
            ("child_units", "DEMO000006", month, 110),
        ]
    return points
