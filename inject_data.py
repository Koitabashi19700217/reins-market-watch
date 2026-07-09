#!/usr/bin/env python3
"""
Stamp reins_data.json into the dashboard's jsx template, between
// @DATA:<name>:start / :end marker comments. The jsx UI code never
needs to be hand-edited when a new month's PDF comes in — only
parse_reins_pdf.py's output changes.

Usage:
    python3 inject_data.py reins_data.json reins_dashboard_prototype.jsx
"""
import json
import re
import sys

COLORS = {"東京都": "NAVY", "神奈川県": "TEAL", "埼玉県": '"#eda100"', "千葉県": "CORAL"}


def js_num(x):
    return f"{x:g}"


def build_pref_series(data):
    lines = ["const prefSeries = {"]
    for pref, d in data["prefectures"].items():
        color = COLORS.get(pref, "GRAY")
        series = ",".join(js_num(v) for v in d["series"])
        entry = f'  {pref}: {{ color: {color}, count: {d["count"]}, countYoy: {js_num(d["countYoy"])}, ' \
                f'price: {d["price"]}, priceYoy: {js_num(d["priceYoy"])},\n' \
                f'    series: [{series}],\n'
        if "listings" in d:
            listings = ",".join(str(v) for v in d["listings"])
            entry += f'    listings: [{listings}],\n' \
                     f'    listingsCount: {d["listingsCount"]}, listingsYoy: {js_num(d["listingsYoy"])} }},\n'
        else:
            entry = entry.rstrip(",\n") + " },\n"
        lines.append(entry)
    lines.append("};")
    return "\n".join(lines)


def build_house_series(data):
    lines = ["const houseSeries = {"]
    for pref, d in data["prefectures"].items():
        if "houseSeries" not in d:
            continue
        vals = ",".join(str(v) for v in d["houseSeries"])
        lines.append(f"  {pref}: [{vals}],")
    lines.append("};")
    return "\n".join(lines)


def build_land_series(data):
    lines = ["const landSeries = {"]
    for pref, d in data["prefectures"].items():
        if "landSeries" not in d:
            continue
        vals = ",".join(str(v) for v in d["landSeries"])
        lines.append(f"  {pref}: [{vals}],")
    lines.append("};")
    return "\n".join(lines)


def build_price_bands(data):
    lines = ["const priceBandsByCategory = {"]
    for cat, prefs in data["priceBandsByCategory"].items():
        lines.append(f"  {cat}: {{")
        for pref, bands in prefs.items():
            items = ", ".join(f'{{ band: "{b["band"]}", pct: {js_num(b["pct"])} }}' for b in bands)
            lines.append(f"    {pref}: [\n      {items},\n    ],")
        lines.append("  },")
    lines.append("};")
    return "\n".join(lines)


def replace_block(text, name, new_body):
    pattern = re.compile(
        rf"(// @DATA:{name}:start\n).*?(\n// @DATA:{name}:end)", re.DOTALL
    )
    if not pattern.search(text):
        raise ValueError(f"Markers for {name} not found in template")
    return pattern.sub(lambda m: m.group(1) + new_body + m.group(2), text)


def build_national_ranking(data):
    ranking = data.get("nationalRanking", {})
    highlight_prefs = ["東京都", "神奈川県", "埼玉県", "千葉県"]
    lines = ["const nationalRankingByCategory = {"]
    for cat, rows in ranking.items():
        top = sorted(rows, key=lambda r: r["priceYoy"], reverse=True)[:10]
        present = {r["pref"] for r in top}
        for hp in highlight_prefs:
            if hp not in present:
                found = next((r for r in rows if r["pref"] == hp), None)
                if found:
                    top.append(found)
        items = ", ".join(f'{{ pref: "{r["pref"]}", yoy: {js_num(r["priceYoy"])} }}' for r in top)
        lines.append(f"  {cat}: [{items}],")
    lines.append("};")
    return "\n".join(lines)


def main(json_path, jsx_path):
    data = json.loads(open(json_path, encoding="utf-8").read())
    text = open(jsx_path, encoding="utf-8").read()
    text = replace_block(text, "prefSeries", build_pref_series(data))
    text = replace_block(text, "houseSeries", build_house_series(data))
    text = replace_block(text, "landSeries", build_land_series(data))
    text = replace_block(text, "priceBandsByCategory", build_price_bands(data))
    text = replace_block(text, "nationalRankingByCategory", build_national_ranking(data))
    open(jsx_path, "w", encoding="utf-8").write(text)
    print(f"Injected fresh data into {jsx_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 inject_data.py <reins_data.json> <dashboard.jsx>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
