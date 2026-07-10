#!/usr/bin/env python3
"""
calc_affordability.py --all --out-csv の結果を読み込んで、
「転入は多いのに届く物件が少ない」「届くのに選ばれていない」といった
ギャップを自動検出し、Markdownの文章レポートにする。

Usage:
    python3 calc_affordability.py --all --out-csv affordability_summary.csv
    python3 generate_report.py affordability_summary.csv --out report_2026-XX.md
"""
import argparse
import csv
from collections import defaultdict
from pathlib import Path

# 「購入検討層」とみなす年齢帯(20代前半・65歳以上は賃貸/相続が主体になりやすいため参考値扱い)
CORE_AGES = ["25-29", "30-34", "35-39", "40-44", "45-49", "50-54"]
CATEGORIES = ["中古マンション", "戸建", "土地"]


def load_rows(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    for r in rows:
        r["転入超過数"] = int(r["転入超過数"]) if r["転入超過数"] not in ("", None) else None
        r["購入可能額(万円)"] = int(r["購入可能額(万円)"])
        for c in CATEGORIES:
            key = f"{c}届く割合(%)"
            r[key] = float(r[key]) if r[key] not in ("", None) else None
    return rows


def build_report(rows, category="中古マンション"):
    key = f"{category}届く割合(%)"
    core = [r for r in rows if r["年齢帯"] in CORE_AGES and r["転入超過数"] is not None and r[key] is not None]

    by_ward = defaultdict(list)
    for r in core:
        by_ward[r["区"]].append(r)

    lines = []
    lines.append(f"# 客層分析レポート({category})")
    lines.append("")
    lines.append("転入超過数(年齢5歳階級別、e-Stat実データ) × 購入可能額(年収・貯蓄からの試算) × 実際の価格帯供給、を突き合わせた自動分析です。")
    lines.append("")

    # 区ごとのハイライト
    lines.append("## 区別サマリー")
    lines.append("")
    for ward, entries in by_ward.items():
        entries_sorted = sorted(entries, key=lambda r: r["転入超過数"], reverse=True)
        top_inflow = entries_sorted[0]
        best_coverage = max(entries, key=lambda r: r[key])
        worst_coverage = min(entries, key=lambda r: r[key])

        lines.append(f"### {ward}")
        lines.append(
            f"- 最も転入超過が多いのは{top_inflow['年齢帯']}歳({top_inflow['転入超過数']:+d}人)。"
            f"この層の購入可能額(約{top_inflow['購入可能額(万円)']:,}万円)で届く{category}の物件は約{top_inflow[key]:.1f}%。"
        )
        if best_coverage["年齢帯"] != top_inflow["年齢帯"]:
            lines.append(
                f"- 最も物件が届きやすいのは{best_coverage['年齢帯']}歳(届く割合約{best_coverage[key]:.1f}%、"
                f"転入超過{best_coverage['転入超過数']:+d}人)。"
            )
        gap = top_inflow[key] - best_coverage[key]
        if top_inflow["年齢帯"] != best_coverage["年齢帯"] and best_coverage[key] - top_inflow[key] >= 10:
            lines.append(
                f"- ⚠️ **転入が最多の層({top_inflow['年齢帯']}歳)より、"
                f"別の層({best_coverage['年齢帯']}歳)の方が実際には物件に届きやすい**状態。"
                f"転入層向けの提案には注意が必要(賃貸需要主体の可能性)。"
            )
        lines.append("")

    # 区をまたいだギャップ検出(転入は多いが供給が薄い、逆に供給は厚いが転入が少ない)
    lines.append("## 区をまたいだギャップ")
    lines.append("")
    ranked_by_inflow = sorted(core, key=lambda r: r["転入超過数"], reverse=True)[:5]
    ranked_by_coverage = sorted(core, key=lambda r: r[key], reverse=True)[:5]

    lines.append("**転入超過数トップ5(コア年齢帯)**")
    for r in ranked_by_inflow:
        lines.append(f"- {r['区']}・{r['年齢帯']}歳: {r['転入超過数']:+d}人(届く割合 約{r[key]:.1f}%)")
    lines.append("")
    lines.append("**届く物件割合トップ5(コア年齢帯)**")
    for r in ranked_by_coverage:
        lines.append(f"- {r['区']}・{r['年齢帯']}歳: 約{r[key]:.1f}%(転入超過 {r['転入超過数']:+d}人)")
    lines.append("")

    inflow_wards = {r["区"] for r in ranked_by_inflow}
    coverage_wards = {r["区"] for r in ranked_by_coverage}
    only_inflow = inflow_wards - coverage_wards
    only_coverage = coverage_wards - inflow_wards
    if only_inflow:
        lines.append(f"- 転入は多いが物件供給が薄い区: **{', '.join(only_inflow)}**(賃貸需要が主体の可能性、要注意)")
    if only_coverage:
        lines.append(f"- 物件は届きやすいが転入が目立たない区: **{', '.join(only_coverage)}**(訴求次第で需要を掘り起こせる可能性)")

    lines.append("")
    lines.append("---")
    lines.append("※ 年収・貯蓄は国税庁/J-FLECの全国平均概算値、購入可能額は返済負担率25%・金利3.13%・35年ローン・頭金充当率50%が前提。")
    lines.append("※ 20代前半・65歳以上は賃貸/相続が主体になりやすいため、コア年齢帯(25〜54歳)に絞って集計。")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--category", default="中古マンション", choices=CATEGORIES)
    ap.add_argument("--out", default="report.md")
    args = ap.parse_args()

    rows = load_rows(args.csv_path)
    report = build_report(rows, args.category)
    Path(args.out).write_text(report, encoding="utf-8")
    print(f"→ {args.out} を書き出しました")
    print()
    print(report)


if __name__ == "__main__":
    main()
