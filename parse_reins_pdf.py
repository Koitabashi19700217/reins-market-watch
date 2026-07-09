#!/usr/bin/env python3
"""
REINS Market Watch (月例速報) PDF -> reins_data.json

Usage:
    python3 parse_reins_pdf.py /path/to/ZMW_YYYYMMdata.pdf /path/to/output/reins_data.json

Re-run this any month a new PDF is downloaded; the JSON output feeds the
React dashboard (reins_dashboard_prototype.jsx) unchanged.
"""
import subprocess
import re
import sys
import json
from pathlib import Path

TARGET_PREFS = ["東京都", "神奈川県", "埼玉県", "千葉県"]

ALL_PREFS = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]

REPORT_HEADERS = {
    "Ⅰ．中古マンションレポート": "中古マンション",
    "Ⅱ．中古戸建住宅レポート": "中古戸建",
    "Ⅲ．新築戸建住宅レポート": "新築戸建",
    "Ⅳ．土地レポート": "土地",
}
SECTION_HEADERS = {
    "１．都道府県別概況": "概況",
    "２．都道府県別価格帯別件数": "価格帯別",
    "３．〔参考〕別集計": "参考別集計",
}
SUBSECTION_HEADERS = {
    "（１）成約状況": "成約状況",
    "（２）新規登録状況": "新規登録状況",
    "（３）在庫状況": "在庫状況",
}

MANSION_BANDS = ["〜1000万", "〜2000万", "〜3000万", "〜4000万", "〜5000万", "〜7000万", "〜1億", "1億〜"]
HOUSE_LAND_BANDS = ["〜1000万", "〜2000万", "〜3000万", "〜4000万", "〜5000万", "〜7000万", "〜1億", "〜2億", "2億〜"]

MONTH_ROW = re.compile(r"^\s*(\d{2}/\d{2}|\d{2})\s+([\-\d,.\s]+?)\s*$")
QUARTER_ROW = re.compile(r"^\d{4}/\d{2}～\d{2}\s+([\d,]+(?:\s+[\d,]+)+)\s*$")
PCT_ROW = re.compile(r"^\s*(\([\s\d.]+\)\s*)+$")
PCT_VAL = re.compile(r"\(\s*([\d.]+)\s*\)")


def pdf_to_text(pdf_path: str) -> str:
    out = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def walk_with_context(text: str):
    """Yield (report, section, subsection, line) for every line, tracking
    which header block we're currently inside."""
    report = section = subsection = None
    for line in text.splitlines():
        stripped = line.strip()
        matched_header = False
        for key, val in REPORT_HEADERS.items():
            if stripped.startswith(key):
                report, section, subsection = val, None, None
                matched_header = True
                break
        if not matched_header:
            for key, val in SECTION_HEADERS.items():
                if stripped.startswith(key):
                    section, subsection = val, None
                    matched_header = True
                    break
        if not matched_header:
            for key, val in SUBSECTION_HEADERS.items():
                if stripped.startswith(key):
                    subsection = val
                    matched_header = True
                    break
        yield report, section, subsection, line


def parse_overview(text: str, prefs):
    """report -> subsection -> pref -> list of {year, month, vals[12]}"""
    result = {}
    current_pref = None
    current_year = None
    ctx_key = None
    for report, section, subsection, line in walk_with_context(text):
        if section != "概況":
            current_pref = None
            continue
        stripped = line.strip()
        m = re.match(r"^○(.+)$", stripped)
        if m:
            name = m.group(1).strip()
            current_pref = name if name in prefs else None
            current_year = None
            continue
        if current_pref is None or report is None or subsection is None:
            continue
        row = MONTH_ROW.match(line)
        if not row:
            continue
        month_tok, nums_tok = row.groups()
        nums_raw = nums_tok.split()
        try:
            nums = [None if x == "-" else float(x.replace(",", "")) for x in nums_raw]
        except ValueError:
            continue
        if len(nums) not in (11, 12):
            continue
        if "/" in month_tok:
            current_year, month = month_tok.split("/")
        else:
            month = month_tok
        if current_year is None:
            continue
        key = (report, subsection, current_pref)
        result.setdefault(report, {}).setdefault(subsection, {}).setdefault(current_pref, [])
        result[report][subsection][current_pref].append({
            "year": current_year, "month": month, "vals": nums
        })
    return result


def parse_price_bands(text: str, prefs):
    """report -> subsection -> pref -> latest quarter pct list"""
    result = {}
    current_pref = None
    buffer_counts_line = None
    for report, section, subsection, line in walk_with_context(text):
        if section != "価格帯別":
            current_pref = None
            continue
        stripped = line.strip()
        m = re.match(r"^○(.+)$", stripped)
        if m:
            name = m.group(1).strip()
            current_pref = name if name in prefs else None
            continue
        if current_pref is None or report is None or subsection is None:
            continue
        if QUARTER_ROW.match(line):
            buffer_counts_line = line
            continue
        if buffer_counts_line is not None and PCT_ROW.match(line.strip()):
            pcts = [float(x) for x in PCT_VAL.findall(line)]
            if len(pcts) >= 2:
                pcts = pcts[:-1]  # drop the trailing 100.0 (計) column
                result.setdefault(report, {}).setdefault(subsection, {})[current_pref] = pcts
            buffer_counts_line = None
    return result


def band_labels_for(report: str):
    return MANSION_BANDS if report == "中古マンション" else HOUSE_LAND_BANDS


# index of (price, priceYoy) within the 11/12-number overview row, per report
PRICE_IDX = {
    "中古マンション": (5, 6),
    "中古戸建": (2, 3),
    "土地": (5, 6),
}


def build_national_ranking(overview, min_count=20):
    """report -> list of {pref, count, price, priceYoy} for the latest month,
    across all 47 prefectures. Prefectures with fewer than min_count
    transactions are excluded — with tiny samples, YoY% swings wildly
    and isn't a meaningful market signal."""
    result = {}
    DASHBOARD_LABEL = {"中古マンション": "中古マンション", "中古戸建": "戸建", "土地": "土地"}
    for report_key in ["中古マンション", "中古戸建", "土地"]:
        contract = overview.get(report_key, {}).get("成約状況", {})
        price_i, yoy_i = PRICE_IDX[report_key]
        rows = []
        for pref in ALL_PREFS:
            pref_rows = contract.get(pref, [])
            if not pref_rows:
                continue
            last = pref_rows[-1]
            vals = last["vals"]
            if len(vals) <= max(price_i, yoy_i):
                continue
            if vals[0] is None or vals[1] is None or vals[price_i] is None or vals[yoy_i] is None:
                continue
            if vals[0] < min_count:
                continue
            rows.append({
                "pref": pref,
                "count": int(vals[0]),
                "countYoy": vals[1],
                "price": int(vals[price_i]),
                "priceYoy": vals[yoy_i],
            })
        rows.sort(key=lambda r: r["priceYoy"], reverse=True)
        result[DASHBOARD_LABEL[report_key]] = rows
    return result


def build_json(pdf_path: str):
    text = pdf_to_text(pdf_path)
    overview = parse_overview(text, ALL_PREFS)
    bands = parse_price_bands(text, TARGET_PREFS)

    out = {"prefectures": {}, "priceBandsByCategory": {}, "nationalRanking": {}}

    # --- mansion overview: 成約状況 + 新規登録状況 ---
    mansion_contract = overview.get("中古マンション", {}).get("成約状況", {})
    mansion_listing = overview.get("中古マンション", {}).get("新規登録状況", {})
    house_contract = overview.get("中古戸建", {}).get("成約状況", {})
    land_contract = overview.get("土地", {}).get("成約状況", {})

    for pref in TARGET_PREFS:
        c_rows = mansion_contract.get(pref, [])
        l_rows = mansion_listing.get(pref, [])
        h_rows = house_contract.get(pref, [])
        d_rows = land_contract.get(pref, [])
        if not c_rows:
            continue
        last = c_rows[-1]
        if last["vals"][0] is None or last["vals"][1] is None or last["vals"][5] is None or last["vals"][6] is None:
            continue
        first = c_rows[0]
        entry = {
            "count": int(last["vals"][0]),
            "countYoy": last["vals"][1],
            "price": int(last["vals"][5]),
            "priceYoy": last["vals"][6],
            "series": [r["vals"][2] for r in c_rows if r["vals"][2] is not None],       # ㎡単価, chronological
            "months": [f'{r["year"]}/{r["month"]}' for r in c_rows],
        }
        if l_rows:
            entry["listingsCount"] = int(l_rows[-1]["vals"][0])
            entry["listingsYoy"] = l_rows[-1]["vals"][1]
            entry["listings"] = [int(r["vals"][0]) for r in l_rows if r["vals"][0] is not None]
        if h_rows:
            entry["houseSeries"] = [int(r["vals"][2]) for r in h_rows if r["vals"][2] is not None]  # 成約価格,万円
        if d_rows:
            entry["landSeries"] = [int(r["vals"][5]) for r in d_rows if r["vals"][5] is not None]  # 成約価格,万円(土地)
        out["prefectures"][pref] = entry

    # --- price bands: 3 categories x target prefs, latest quarter ---
    for report_key, dashboard_key in [("中古マンション", "中古マンション"), ("中古戸建", "戸建"), ("土地", "土地")]:
        cat_data = bands.get(report_key, {}).get("成約状況", {})
        labels = band_labels_for(report_key)
        cat_out = {}
        for pref in TARGET_PREFS:
            pcts = cat_data.get(pref)
            if not pcts:
                continue
            n = min(len(labels), len(pcts))
            cat_out[pref] = [{"band": labels[i], "pct": pcts[i]} for i in range(n)]
        if cat_out:
            out["priceBandsByCategory"][dashboard_key] = cat_out

    out["nationalRanking"] = build_national_ranking(overview)

    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 parse_reins_pdf.py <input.pdf> <output.json>")
        sys.exit(1)
    data = build_json(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {sys.argv[2]}")
    print(f"Prefectures with overview data: {list(data['prefectures'].keys())}")
    print(f"Categories with price-band data: {list(data['priceBandsByCategory'].keys())}")
    for cat, rows in data.get("nationalRanking", {}).items():
        print(f"National ranking ({cat}): {len(rows)} prefectures")
