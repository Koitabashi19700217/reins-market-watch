#!/usr/bin/env python3
"""
「①転入転出 × ②購入可能額 × ③実際の価格帯供給」を自動で突き合わせるスクリプト。

必要なファイル(同じフォルダに置く):
    demographics_config.json   - 年齢帯別の年収・貯蓄の仮定値
    reins_migration_data.json  - parse_migration_data.py の出力(区・年齢帯別 転入超過数)
    reins_kanagawa_data.json   - parse_kanagawa_ward.py の出力(区・カテゴリ別 価格帯分布)

Usage:
    # 1区・1年齢帯だけ見る
    python3 calc_affordability.py --ward 港北区 --age 30-34

    # 全区×全年齢帯の一覧表を出す(サマリー)
    python3 calc_affordability.py --all

    # 返済負担率など前提を変えて試算
    python3 calc_affordability.py --ward 都筑区 --age 35-39 --ratio 30 --dpr 70
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

MANSION_BAND_UPPER = {
    "~1,000万": 1000, "1,000~2,000万": 2000, "2,000~3,000万": 3000,
    "3,000~4,000万": 4000, "4,000~5,000万": 5000, "5,000~6,000万": 6000,
    "6,000~8,000万": 8000, "8,000~1億": 10000, "1億超": 10**9,
}
HOUSE_BAND_UPPER = {
    "〜2,000万": 2000, "2,000〜3,000万": 3000, "3,000〜4,000万": 4000,
    "4,000〜5,000万": 5000, "5,000〜6,000万": 6000, "6,000〜7,000万": 7000,
    "7,000〜8,000万": 8000, "8,000〜1億": 10000, "1億超": 10**9,
}
LAND_BAND_UPPER = {
    "〜2,000万": 2000, "2,000〜3,000万": 3000, "3,000〜5,000万": 5000,
    "5,000〜8,000万": 8000, "8,000〜1.2億": 12000, "1.2億〜2億": 20000, "2億超": 10**9,
}
CATEGORY_BANDS = {"中古マンション": MANSION_BAND_UPPER, "戸建": HOUSE_BAND_UPPER, "土地": LAND_BAND_UPPER}


def load_json(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def purchase_power(income, savings, ratio, rate, years, dpr, emg_months, take_home_ratio, living_cost_ratio, fee_ratio):
    living_cost = income * take_home_ratio * living_cost_ratio / 12
    emergency = living_cost * emg_months
    available_savings = max(savings - emergency, 0)
    down_payment = available_savings * dpr / 100
    monthly_capacity = income * 10000 * ratio / 100 / 12
    monthly_rate = rate / 100 / 12
    n = years * 12
    if monthly_rate > 0:
        loan = monthly_capacity * (1 - (1 + monthly_rate) ** -n) / monthly_rate
    else:
        loan = monthly_capacity * n
    loan_man = loan / 10000
    total = (loan_man + down_payment) / (1 - fee_ratio)
    return round(total), round(down_payment), round(loan_man), round(monthly_capacity / 10000, 1)


def affordable_pct(bands, threshold, band_map):
    return sum(b["pct"] for b in bands if band_map.get(b["band"], 10**9) <= threshold)


def analyze(ward, age, demo, migration, wards_data, params):
    profile = demo["ageProfiles"].get(age)
    if not profile:
        raise ValueError(f"年齢帯 '{age}' は demographics_config.json に未登録です")
    p = params
    power, down, loan, monthly = purchase_power(
        profile["income"], profile["savings"],
        p["repaymentRatio"], p["interestRate"], p["years"], p["downPaymentRatio"],
        p["emergencyFundMonths"], p["takeHomeRatio"], p["livingCostRatio"], p["acquisitionFeeRatio"],
    )
    net_migration = None
    if ward in migration:
        net_migration = migration[ward]["byAge"].get(age)

    coverage = {}
    if ward in wards_data:
        for cat, band_map in CATEGORY_BANDS.items():
            bands = wards_data[ward].get(cat, {}).get("priceBands", [])
            if bands:
                coverage[cat] = round(affordable_pct(bands, power, band_map), 1)

    return {
        "ward": ward, "age": age, "income": profile["income"], "savings": profile["savings"],
        "purchasePower": power, "downPayment": down, "loanAmount": loan, "monthlyPayment": monthly,
        "netMigration": net_migration, "affordableCoverage": coverage,
    }


def print_result(r):
    print(f"\n=== {r['ward']} ・ {r['age']}歳 ===")
    print(f"  前提: 年収{r['income']}万円 / 貯蓄{r['savings']}万円")
    print(f"  購入可能額: {r['purchasePower']:,}万円 (頭金{r['downPayment']:,}万円 + 借入{r['loanAmount']:,}万円, 月々返済{r['monthlyPayment']}万円)")
    if r["netMigration"] is not None:
        sign = "+" if r["netMigration"] >= 0 else ""
        print(f"  転入超過数: {sign}{r['netMigration']}人")
    for cat, pct in r["affordableCoverage"].items():
        print(f"  {cat}: 届く物件割合 約{pct}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ward")
    ap.add_argument("--age")
    ap.add_argument("--all", action="store_true", help="全区×全年齢帯(転入転出データがある年齢帯のみ)の一覧を出す")
    ap.add_argument("--ratio", type=float, help="返済負担率(%)で前提を上書き")
    ap.add_argument("--rate", type=float, help="金利(%)で前提を上書き")
    ap.add_argument("--years", type=int, help="返済年数で前提を上書き")
    ap.add_argument("--dpr", type=float, help="頭金充当率(%)で前提を上書き")
    ap.add_argument("--out-csv", help="結果をCSVに書き出す(--all使用時)")
    args = ap.parse_args()

    demo = load_json("demographics_config.json")
    migration = load_json("reins_migration_data.json")
    wards_data = load_json("reins_kanagawa_data.json")["wards"]

    params = dict(demo["defaultParams"])
    if args.ratio is not None:
        params["repaymentRatio"] = args.ratio
    if args.rate is not None:
        params["interestRate"] = args.rate
    if args.years is not None:
        params["years"] = args.years
    if args.dpr is not None:
        params["downPaymentRatio"] = args.dpr

    if args.all:
        rows = []
        for ward in wards_data:
            if ward not in migration:
                continue
            for age in demo["ageProfiles"]:
                if age not in migration[ward]["byAge"]:
                    continue
                r = analyze(ward, age, demo, migration, wards_data, params)
                rows.append(r)
                print_result(r)
        if args.out_csv:
            import csv
            cats = list(CATEGORY_BANDS.keys())
            with open(args.out_csv, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["区", "年齢帯", "年収(万円)", "貯蓄(万円)", "購入可能額(万円)", "転入超過数"] + [f"{c}届く割合(%)" for c in cats])
                for r in rows:
                    w.writerow([r["ward"], r["age"], r["income"], r["savings"], r["purchasePower"], r["netMigration"]]
                               + [r["affordableCoverage"].get(c, "") for c in cats])
            print(f"\n→ {args.out_csv} に書き出しました")
        return

    if not args.ward or not args.age:
        print("Usage: --ward <区名> --age <年齢帯> または --all を指定してください")
        return
    r = analyze(args.ward, args.age, demo, migration, wards_data, params)
    print_result(r)


if __name__ == "__main__":
    main()
