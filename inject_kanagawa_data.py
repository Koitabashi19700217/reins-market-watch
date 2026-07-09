#!/usr/bin/env python3
"""
Stamp reins_kanagawa_data.json (区単位データ) into the dashboard jsx,
between // @DATA:wardData:start / :end markers. Independent from
inject_data.py (県単位データ) — run both after updating either source.

Usage:
    python3 inject_kanagawa_data.py reins_kanagawa_data.json reins_dashboard_prototype.jsx
"""
import json
import re
import sys


def js_num(x):
    if x is None:
        return "null"
    return f"{x:g}" if isinstance(x, (int, float)) else "null"


def build_table(t):
    if not t:
        return "null"
    headers = json.dumps(t["headers"], ensure_ascii=False)
    rows = json.dumps(t["rows"], ensure_ascii=False)
    return f'{{ headers: {headers}, rows: {rows} }}'


def build_ward_data(data):
    lines = ["const wardData = {"]
    for ward, cats in data.get("wards", {}).items():
        lines.append(f'  "{ward}": {{')
        for cat, d in cats.items():
            ov = d["overview"]
            bands = ", ".join(
                f'{{ band: "{b["band"]}", pct: {js_num(b["pct"])}, count: {b["count"]} }}'
                for b in d["priceBands"]
            )
            stations = ", ".join(
                f'{{ station: "{s["station"]}", count: {s["count"]}, avgPrice: {js_num(s["avgPrice"])}, avgSqmPrice: {js_num(s["avgSqmPrice"])} }}'
                for s in d["stationRanking"]
            )
            lines.append(f'    "{cat}": {{')
            lines.append(f'      overview: {{ count: {ov["count"]}, avgPrice: {js_num(ov["avgPrice"])}, avgSqmPrice: {js_num(ov["avgSqmPrice"])}, period: "{ov["period"] or ""}" }},')
            lines.append(f'      priceBands: [{bands}],')
            lines.append(f'      stationRanking: [{stations}],')
            lines.append(f'      ageBands: {build_table(d.get("ageBands"))},')
            lines.append(f'      tsuboBands: {build_table(d.get("tsuboBands"))},')
            lines.append(f'      walkBands: {build_table(d.get("walkBands"))},')
            lines.append(f'    }},')
        lines.append("  },")
    lines.append("};")
    return "\n".join(lines)


def replace_block(text, name, new_body):
    pattern = re.compile(rf"(// @DATA:{name}:start\n).*?(\n// @DATA:{name}:end)", re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"Markers for {name} not found in template")
    return pattern.sub(lambda m: m.group(1) + new_body + m.group(2), text)


def main(json_path, jsx_path):
    data = json.loads(open(json_path, encoding="utf-8").read())
    text = open(jsx_path, encoding="utf-8").read()
    text = replace_block(text, "wardData", build_ward_data(data))
    open(jsx_path, "w", encoding="utf-8").write(text)
    print(f"Injected ward data into {jsx_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 inject_kanagawa_data.py <reins_kanagawa_data.json> <dashboard.jsx>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
