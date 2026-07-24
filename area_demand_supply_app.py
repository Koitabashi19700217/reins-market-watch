# -*- coding: utf-8 -*-
"""
area_demand_supply_app.py
エリア需給マッチング診断（プロトタイプ v0.2）
================================================================
「そのエリアにどんな年代がどれだけ流入していて、その層がいくらまで
無理なく買えるか」を、実際の市場価格帯分布と突き合わせて見るツール。

事例比較法（過去の成約実績の後追い）だけでなく、
需要側（流入層の年収・貯蓄）から見た価格の妥当性を補完する狙い。

★ 計算ロジックは calc_affordability.py の analyze() / purchase_power() を
  そのまま呼び出す（ロジックの二重管理を避けるため、Streamlit側では計算式を持たない）。

必要なファイル（すべて同じフォルダに置く）:
    calc_affordability.py       - 計算ロジック本体
    demographics_config.json    - 年齢帯別の年収・貯蓄の仮定値
    reins_migration_data.json   - 区・年齢帯別 純流入数
    reins_kanagawa_data.json    - 区・カテゴリ別 価格帯分布（REINS、区単位）

起動: streamlit run area_demand_supply_app.py
"""

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st
import altair as alt

import calc_affordability as ca

import re

DB_PATH = Path(__file__).resolve().parent / "reins_history.db"


def band_sort_key(label):
    """価格帯ラベル文字列から上限金額（万円換算）を抽出して並べ替えキーにする。
    辞書（CATEGORY_BANDS）との文字列完全一致に頼らないため、
    全角波ダッシュ「〜」と半角チルダ「~」の表記ゆれがあっても機能する。"""
    if "超" in label:
        return 10 ** 9
    values = []
    for num_str, unit in re.findall(r"([\d,]+)\s*(億|万)", label):
        n = int(num_str.replace(",", ""))
        if unit == "億":
            n *= 10000
        values.append(n)
    return max(values) if values else 10 ** 9

st.set_page_config(page_title="エリア需給マッチング診断", page_icon="🧭", layout="wide")

AGE_BAND_ORDER = [
    "0-4", "5-9", "10-14", "15-19", "20-24", "25-29", "30-34", "35-39",
    "40-44", "45-49", "50-54", "55-59", "60-64", "65-69", "70-74",
    "75-79", "80-84", "85-89", "90+",
]
# 購入層として意味のある年代帯（住宅取得の主力層）に絞る
# 25-29歳は住宅取得世帯の実態との乖離が大きいため試算対象外
BUYER_AGE_BANDS = ["30-34", "35-39", "40-44", "45-49", "50-54"]
CATEGORIES = list(ca.CATEGORY_BANDS.keys())  # 中古マンション / 戸建 / 土地


@st.cache_data
def load_all():
    demo = ca.load_json("demographics_config.json")
    migration = ca.load_json("reins_migration_data.json")
    wards_data = ca.load_json("reins_kanagawa_data.json")["wards"]
    return demo, migration, wards_data


@st.cache_data
def load_db(ward):
    """reinfolib/e-Stat由来の拡張データ(reins_history.db)を区単位で読み込む。
    DB未生成・未取得時は空DataFrameを返し、既存機能には影響させない。"""
    empty = {}
    if not DB_PATH.exists():
        return empty
    conn = sqlite3.connect(str(DB_PATH))
    try:
        out = {
            "price_bands": pd.read_sql_query(
                "SELECT * FROM v_reinfolib_price_bands WHERE ward=? ORDER BY report_quarter",
                conn, params=(ward,)),
            "land_price": pd.read_sql_query(
                "SELECT * FROM v_land_price_summary WHERE ward=? ORDER BY target_year",
                conn, params=(ward,)),
            "zone_mix": pd.read_sql_query(
                "SELECT * FROM v_zone_mix WHERE ward=? ORDER BY pct_of_ward DESC",
                conn, params=(ward,)),
            "dynamics": pd.read_sql_query(
                "SELECT survey_year, indicator, value FROM population_dynamics "
                "WHERE ward=? ORDER BY survey_year", conn, params=(ward,)),
            "households": pd.read_sql_query(
                "SELECT survey_year, households, population, avg_household_size "
                "FROM household_trends WHERE ward=? ORDER BY survey_year", conn, params=(ward,)),
        }
    finally:
        conn.close()
    return out


demo, migration, wards_data = load_all()
default_params = dict(demo["defaultParams"])

st.title("🧭 エリア需給マッチング診断（プロトタイプ）")
st.caption(
    "事例比較法（過去の成約実績）だけでなく、そのエリアに流入している層の年収・貯蓄から見た"
    "「本当に買える価格帯」を突き合わせる試み。数値は概算・プロトタイプ版です。"
)

with st.sidebar:
    st.header("条件設定")
    wards = sorted(set(migration.keys()) & set(wards_data.keys()))
    ward = st.selectbox("区", wards, index=wards.index("港北区") if "港北区" in wards else 0)
    category = st.selectbox("物件カテゴリ", CATEGORIES)

    st.divider()
    st.caption("借入可能額試算パラメータ（demographics_config.json の defaultParams）")
    ratio = st.slider("返済負担率上限（%）", 15.0, 35.0, float(default_params["repaymentRatio"]), 0.5)
    rate = st.number_input("借入金利（%）", 0.5, 8.0, float(default_params["interestRate"]), 0.01)
    years = st.slider("返済期間（年）", 10, 40, int(default_params["years"]))
    dpr = st.slider("頭金に回す貯蓄割合（%）", 0, 100, int(default_params["downPaymentRatio"]))

    params = dict(default_params)
    params["repaymentRatio"] = ratio
    params["interestRate"] = rate
    params["years"] = years
    params["downPaymentRatio"] = dpr

db = load_db(ward)

# ── 1. 選択エリアの年代別純流入 ──────────────────────────────
st.header(f"① {ward} の年代別純流入")

mig_by_age = migration[ward]["byAge"]
mig_df = pd.DataFrame([
    {"age": age, "net_migration": mig_by_age[age]}
    for age in AGE_BAND_ORDER if age in mig_by_age
])
mig_chart = (
    alt.Chart(mig_df)
    .mark_bar(color="#7EC8FA")
    .encode(
        x=alt.X("age:N", sort=AGE_BAND_ORDER, title=None),
        y=alt.Y("net_migration:Q", title=None),
        tooltip=["age", "net_migration"],
    )
)
st.altair_chart(mig_chart, use_container_width=True)
st.caption("プラス＝転入超過、マイナス＝転出超過。出典：住民基本台帳人口移動報告ベース。")

dynamics_df = db.get("dynamics", pd.DataFrame())
households_df = db.get("households", pd.DataFrame())
if not dynamics_df.empty or not households_df.empty:
    with st.expander(f"{ward}の人口動態・世帯数の年次推移（e-Stat）を見る"):
        if not dynamics_df.empty:
            piv = dynamics_df.pivot_table(index="survey_year", columns="indicator", values="value")
            # 社会増減（転入超過）と自然増減（出生－死亡）はデータの取れる年数がそもそも違う
            # （転入・転出者数の総数は2018年以降しか公表されておらず、出生・死亡数より歴史が短い）。
            # 1本の折れ線に混ぜると期間の短い系列だけ途中から始まり、急な変動に見えて誤解を招くため、
            # 期間が異なる別系列として個別に描く。
            natural = pd.DataFrame({"year": piv.index})
            if {"出生数", "死亡数"} <= set(piv.columns):
                natural["自然増減"] = (piv["出生数"] - piv["死亡数"]).values
            natural = natural.dropna()
            social = pd.DataFrame({"year": piv.index})
            if {"転入者数", "転出者数"} <= set(piv.columns):
                social["社会増減"] = (piv["転入者数"] - piv["転出者数"]).values
            social = social.dropna()

            col_a, col_b = st.columns(2)
            with col_a:
                if not natural.empty:
                    st.caption(f"自然増減（出生－死亡）　{int(natural['year'].min())}〜{int(natural['year'].max())}年")
                    st.altair_chart(
                        alt.Chart(natural).mark_line(point=True, color="#7EC8FA").encode(
                            x=alt.X("year:O", title=None),
                            y=alt.Y("自然増減:Q"),
                            tooltip=["year", "自然増減"],
                        ), use_container_width=True)
            with col_b:
                if not social.empty:
                    st.caption(f"社会増減（転入超過、転入者数－転出者数）　{int(social['year'].min())}〜{int(social['year'].max())}年")
                    st.altair_chart(
                        alt.Chart(social).mark_line(point=True, color="#B98CE8").encode(
                            x=alt.X("year:O", title=None),
                            y=alt.Y("社会増減:Q"),
                            tooltip=["year", "社会増減"],
                        ), use_container_width=True)
            st.caption(
                "自然増減＝出生数－死亡数、社会増減＝転入者数－転出者数。出典：社会・人口統計体系（総務省）。"
                "社会増減は転入・転出者数（総数）の公表が2018年以降のため、自然増減より期間が短くなっています。"
            )
        if not households_df.empty:
            st.altair_chart(
                alt.Chart(households_df).mark_line(point=True, color="#F0A868").encode(
                    x=alt.X("survey_year:O", title=None),
                    y=alt.Y("avg_household_size:Q", title="1世帯あたり人員"),
                    tooltip=["survey_year", "households", "population", "avg_household_size"],
                ), use_container_width=True)
            st.caption("1世帯あたり人員の推移（国勢調査年のみ）。核家族化・単身化の進行度合いの参考値。")

# ── 2. 購入主力層の借入可能額試算（calc_affordability.analyze を使用）──
st.header("② 購入主力層（30〜54歳）の借入可能額試算")

results = []
for age in BUYER_AGE_BANDS:
    if age not in demo["ageProfiles"]:
        continue
    r = ca.analyze(ward, age, demo, migration, wards_data, params)
    results.append(r)

table_rows = []
for r in results:
    row = {
        "年代": r["age"],
        f"{ward}純流入": r["netMigration"],
        "年収(万円)": r["income"],
        "貯蓄(万円)": r["savings"],
        "借入可能額(万円)": r["loanAmount"],
        "頭金(万円)": r["downPayment"],
        "購入可能額(万円)": r["purchasePower"],
        "月々返済(万円)": r["monthlyPayment"],
    }
    for cat in CATEGORIES:
        row[f"{cat}届く割合(%)"] = r["affordableCoverage"].get(cat, None)
    table_rows.append(row)

result_df = pd.DataFrame(table_rows)
st.dataframe(result_df, use_container_width=True, hide_index=True)
st.caption(
    "計算式は calc_affordability.py の purchase_power() に準拠："
    "生活防衛資金（手取り月額生活費 × emergencyFundMonths）を貯蓄から控除した上で頭金に充当し、"
    "額面年収 × 返済負担率上限 ÷ 12 を月返済上限として元利均等返済から借入可能額を逆算。"
    "購入可能額 = (借入可能額＋頭金) ÷ (1－購入諸費用率)。"
)

# ── 3. 需給マッチング要約 ──────────────────────────────
st.header("③ 需給マッチング要約")

if not result_df.empty:
    valid_mig = result_df[result_df[f"{ward}純流入"].notna()]
    if not valid_mig.empty:
        best_row = valid_mig.loc[valid_mig[f"{ward}純流入"].idxmax()]
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric(f"{ward}で最も流入が多い年代", best_row["年代"], f"純流入 {best_row[f'{ward}純流入']:,}人")
        with col2:
            st.metric("その年代の購入可能額", f"{best_row['購入可能額(万円)']:,.0f}万円")
        with col3:
            pct = best_row.get(f"{category}届く割合(%)")
            st.metric(f"{ward}（{category}）でこの価格以下の物件割合", f"{pct:.1f}%" if pct is not None else "—")

    st.markdown(f"#### {ward}の価格帯分布（{category}、REINS区単位データ）")
    bands = wards_data[ward].get(category, {}).get("priceBands", [])
    if bands:
        band_df = pd.DataFrame(bands)
        band_order = sorted(band_df["band"].unique(), key=band_sort_key)
        chart = (
            alt.Chart(band_df)
            .mark_bar(color="#7EC8FA")
            .encode(
                x=alt.X("band:N", sort=band_order, title=None),
                y=alt.Y("pct:Q", title=None),
                tooltip=["band", "pct"],
            )
        )
        st.altair_chart(chart, use_container_width=True)
        overview = wards_data[ward][category].get("overview", {})
        if overview:
            st.caption(
                f"対象期間：{overview.get('period', '—')} ／ 件数：{overview.get('count', '—')}件 ／ "
                f"平均価格：{overview.get('avgPrice', '—'):,}万円 ／ 平均㎡単価：{overview.get('avgSqmPrice', '—')}万円/㎡"
            )
    else:
        st.info(f"{ward}の{category}データがありません。")

    price_bands_df = db.get("price_bands", pd.DataFrame())
    if not price_bands_df.empty:
        cat_bands = price_bands_df[price_bands_df["category"] == category]
        if not cat_bands.empty:
            latest_q = cat_bands["report_quarter"].max()
            latest = cat_bands[cat_bands["report_quarter"] == latest_q]
            st.markdown(f"#### {ward}の実際の取引価格帯分布（{category}、reinfolib実データ・{latest_q}）")
            band_order2 = sorted(latest["band"].unique(), key=band_sort_key)
            chart2 = (
                alt.Chart(latest)
                .mark_bar(color="#B98CE8")
                .encode(
                    x=alt.X("band:N", sort=band_order2, title=None),
                    y=alt.Y("pct:Q", title=None),
                    tooltip=["band", "pct", "n"],
                )
            )
            st.altair_chart(chart2, use_container_width=True)
            st.caption(
                f"件数：{int(latest['n'].sum())}件（{latest_q}、国交省 不動産情報ライブラリ 実際の取引価格情報より）。"
                "上のREINS集計（成約ベース）と異なり、こちらは登録された実取引の申告価格ベース。"
            )

# ── 4. 地価公示・用途地域 ──────────────────────────────
land_price_df = db.get("land_price", pd.DataFrame())
zone_mix_df = db.get("zone_mix", pd.DataFrame())
if not land_price_df.empty or not zone_mix_df.empty:
    st.header(f"④ {ward}の地価公示・用途地域")

    if not land_price_df.empty:
        st.markdown("#### 地価公示・地価調査の平均価格推移")
        chart4 = (
            alt.Chart(land_price_df)
            .mark_line(point=True)
            .encode(
                x=alt.X("target_year:O", title=None),
                y=alt.Y("avg_price_sqm:Q", title="平均価格（円/㎡）"),
                color=alt.Color("use_category:N", title="用途"),
                tooltip=["target_year", "use_category", "avg_price_sqm", "avg_yoy_pct", "points"],
            )
        )
        st.altair_chart(chart4, use_container_width=True)
        st.caption("出典：国交省 不動産情報ライブラリ 地価公示・都道府県地価調査。地点数の少ない用途区分は年による振れが大きい点に留意。")

    if not zone_mix_df.empty:
        st.markdown("#### 用途地域の構成比（面積ベース）")
        chart5 = (
            alt.Chart(zone_mix_df)
            .mark_bar()
            .encode(
                x=alt.X("pct_of_ward:Q", title="区面積に占める割合(%)"),
                y=alt.Y("use_area:N", sort="-x", title=None),
                color=alt.Color("use_group:N", title=None,
                                scale=alt.Scale(domain=["住居系", "商業系", "工業系", "その他"],
                                                range=["#7EC8FA", "#F0A868", "#B98CE8", "#AAAAAA"])),
                tooltip=["use_area", "pct_of_ward", "avg_floor_area_ratio", "avg_coverage_ratio"],
            )
        )
        st.altair_chart(chart5, use_container_width=True)
        st.caption(
            "出典：国交省 不動産情報ライブラリ 都市計画決定GISデータ（用途地域）。"
            "ベクトルタイル由来のポリゴン面積の近似値であり、行政公表の正式面積とは一致しない場合があります。"
        )

st.divider()
st.caption(
    "本ツールはプロトタイプです。年齢帯別の年収・貯蓄は demographics_config.json の仮定値であり、"
    "30〜50代は国交省「住宅市場動向調査」の住宅取得世帯（一次取得者）実データ、"
    "60歳以上は国税庁・J-FLEC統計に基づきます。いずれも実際の個々の購入者の状況とは異なります。"
    "あくまで「層としての傾向」の参考値としてご利用ください。"
)
