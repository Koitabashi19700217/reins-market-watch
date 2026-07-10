import json
import sys
import tempfile
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parse_reins_pdf
import parse_kanagawa_ward
import calc_affordability
import generate_report

NAVY = "#1F3864"
TEAL = "#0F6E56"
GOLD = "#EDA100"
CORAL = "#D85A30"
GRAY = "#8FBFAE"

PREF_COLORS = {"東京都": NAVY, "神奈川県": TEAL, "埼玉県": GOLD, "千葉県": CORAL}

st.set_page_config(page_title="REINS Market Watch", page_icon="🏙️", layout="wide")


@st.cache_data
def load_default_data():
    market = json.loads((HERE / "reins_data.json").read_text(encoding="utf-8"))
    ward_path = HERE / "reins_kanagawa_data.json"
    wards = json.loads(ward_path.read_text(encoding="utf-8"))["wards"] if ward_path.exists() else {}
    return market, wards


@st.cache_data
def load_customer_data():
    migration_path = HERE / "reins_migration_data.json"
    demo_path = HERE / "demographics_config.json"
    migration = json.loads(migration_path.read_text(encoding="utf-8")) if migration_path.exists() else {}
    demo = json.loads(demo_path.read_text(encoding="utf-8")) if demo_path.exists() else None
    return migration, demo


@st.cache_data(show_spinner="PDFを解析しています…")
def parse_uploaded_pdf(file_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp.flush()
        return parse_reins_pdf.build_json(tmp.name)


@st.cache_data(show_spinner="区データを解析しています…")
def parse_uploaded_ward(ward_name, mansion_bytes, house_bytes, land_bytes):
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = {}
        for label, data in [("mansion", mansion_bytes), ("house", house_bytes), ("land", land_bytes)]:
            if data is None:
                continue
            p = Path(tmpdir) / f"{label}.xlsx"
            p.write_bytes(data)
            paths[label] = str(p)
        out_path = Path(tmpdir) / "out.json"
        ward_data = {}
        if "mansion" in paths:
            ward_data["中古マンション"] = parse_kanagawa_ward.parse_category(paths["mansion"])
        if "house" in paths:
            ward_data["戸建"] = parse_kanagawa_ward.parse_category(paths["house"])
        if "land" in paths:
            ward_data["土地"] = parse_kanagawa_ward.parse_category(paths["land"])
        return ward_data


if "market" not in st.session_state:
    st.session_state.market, st.session_state.wards = load_default_data()

with st.sidebar:
    st.markdown("### データ更新")
    st.caption("新しい月のPDFや区のExcelをアップロードすると、その場で反映されます。GitHubへのpushは不要です。")

    with st.expander("① 月次PDFを更新", expanded=False):
        pdf_file = st.file_uploader("REINS Market Watch PDF", type=["pdf"], key="pdf_upload")
        if pdf_file is not None:
            st.session_state.market = parse_uploaded_pdf(pdf_file.getvalue())
            st.success("反映しました")
            st.download_button(
                "この内容をJSONで保存(バックアップ用)",
                data=json.dumps(st.session_state.market, ensure_ascii=False, indent=2),
                file_name="reins_data.json", mime="application/json",
            )

    with st.expander("② 区データ(Excel)を追加・更新", expanded=False):
        ward_name = st.text_input("区名", placeholder="例: 川崎区")
        mansion_f = st.file_uploader("マンション成約データ.xlsx", type=["xlsx"], key="w_mansion")
        house_f = st.file_uploader("戸建成約データ.xlsx", type=["xlsx"], key="w_house")
        land_f = st.file_uploader("土地成約データ.xlsx", type=["xlsx"], key="w_land")
        if st.button("取り込む", disabled=not ward_name or not (mansion_f or house_f or land_f)):
            st.session_state.wards[ward_name] = parse_uploaded_ward(
                ward_name,
                mansion_f.getvalue() if mansion_f else None,
                house_f.getvalue() if house_f else None,
                land_f.getvalue() if land_f else None,
            )
            st.success(f"{ward_name} を反映しました")
            st.download_button(
                "全区データをJSONで保存(バックアップ用)",
                data=json.dumps({"wards": st.session_state.wards}, ensure_ascii=False, indent=2),
                file_name="reins_kanagawa_data.json", mime="application/json",
            )

market = st.session_state.market
wards = st.session_state.wards
migration, demo = load_customer_data()

st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem; max-width: 1100px;}
    div[data-testid="stMetricValue"] {font-family: monospace;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.caption("REINS MARKET WATCH")
title_ph = st.empty()

tab_national, tab_pref, tab_customer = st.tabs(["全国", "都道府県", "客層分析"])

CATEGORIES = list(market["priceBandsByCategory"].keys())  # 中古マンション / 戸建 / 土地

# ---------------------------------------------------------------- 全国 tab
with tab_national:
    category_n = st.radio("カテゴリ", CATEGORIES, horizontal=True, key="cat_national")
    title_ph.title(f"{category_n}成約状況 2026年5月度")

    ranking = market["nationalRanking"].get(category_n, [])
    ranking_sorted = sorted(ranking, key=lambda r: r["priceYoy"], reverse=True)
    top = ranking_sorted[:10]
    present = {r["pref"] for r in top}
    for hp in ["東京都", "神奈川県", "埼玉県", "千葉県"]:
        if hp not in present:
            found = next((r for r in ranking_sorted if r["pref"] == hp), None)
            if found:
                top.append(found)
    df = pd.DataFrame(top)
    if not df.empty:
        df = df.sort_values("priceYoy", ascending=True)
        colors = [PREF_COLORS.get(p, GRAY) for p in df["pref"]]
        fig = go.Figure(go.Bar(
            x=df["priceYoy"], y=df["pref"], orientation="h",
            marker_color=colors,
            hovertemplate="%{y}: 前年比 %{x:+.1f}%<extra></extra>",
        ))
        label = "成約価格 前年比" if category_n in ("戸建", "土地") else "㎡単価 前年比"
        fig.update_layout(
            title=f"{label}が高い都道府県(上位10県 + 1都3県)",
            xaxis_title="前年比(%)", height=480, margin=dict(l=10, r=10, t=40, b=10),
        )
        st.plotly_chart(fig, use_container_width=True)

        legend_cols = st.columns(len(PREF_COLORS) + 1)
        for i, (p, c) in enumerate(PREF_COLORS.items()):
            legend_cols[i].markdown(f'<span style="color:{c}">■</span> {p}', unsafe_allow_html=True)
        legend_cols[-1].markdown(f'<span style="color:{GRAY}">■</span> その他の県', unsafe_allow_html=True)

    st.caption("※ REINS Market Watch 全47都道府県から自動抽出。件数20件未満の県は前年比が振れやすいため除外。")

# ---------------------------------------------------------------- 都道府県 tab
with tab_pref:
    prefs = list(market["prefectures"].keys())
    selected_pref = st.radio("都道府県", prefs, horizontal=True, key="pref_select")
    category_p = st.radio("カテゴリ", CATEGORIES, horizontal=True, key="cat_pref")

    pdata = market["prefectures"][selected_pref]
    col1, col2, col3 = st.columns(3)
    col1.metric("成約件数", f'{pdata["count"]:,}件', f'{pdata["countYoy"]:+.1f}%')
    col2.metric("成約価格", f'{pdata["price"]:,}万円', f'{pdata["priceYoy"]:+.1f}%')
    if "listingsCount" in pdata:
        col3.metric("新規登録件数", f'{pdata["listingsCount"]:,}件', f'{pdata["listingsYoy"]:+.1f}%')

    # --- price/量 trend across 1都3県, series depends on category ---
    months = pdata["months"]
    fig2 = go.Figure()
    for p in prefs:
        pd_ = market["prefectures"][p]
        if category_p == "中古マンション":
            series = pd_["series"]
        elif category_p == "戸建":
            series = pd_.get("houseSeries", [])
        else:
            series = pd_.get("landSeries", [])
        if not series:
            continue
        fig2.add_trace(go.Scatter(
            x=months, y=series, mode="lines+markers", name=p,
            line=dict(color=PREF_COLORS.get(p, GRAY), width=5 if p == selected_pref else 2.5),
            marker=dict(size=5 if p == selected_pref else 3),
            opacity=1 if p == selected_pref else 0.6,
        ))
    ylabel = "㎡単価(万円)" if category_p == "中古マンション" else "成約価格(万円)"
    fig2.update_layout(title=f"{ylabel}推移 — 1都3県比較", height=340, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig2, use_container_width=True)

    if category_p == "中古マンション" and "listings" in pdata:
        fig3 = go.Figure(go.Bar(x=months, y=pdata["listings"], marker_color=PREF_COLORS.get(selected_pref, TEAL)))
        fig3.update_layout(title=f"新規登録件数推移 — {selected_pref}", height=260, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig3, use_container_width=True)

    bands = market["priceBandsByCategory"].get(category_p, {}).get(selected_pref, [])
    if bands:
        bdf = pd.DataFrame(bands)
        fig4 = go.Figure(go.Bar(
            x=bdf["pct"], y=bdf["band"], orientation="h",
            marker_color=PREF_COLORS.get(selected_pref, TEAL),
        ))
        fig4.update_layout(
            title=f"価格帯別 成約構成比(%) — {selected_pref}・{category_p}(2026年1〜3月期)",
            height=280, margin=dict(l=10, r=10, t=40, b=10),
        )
        st.plotly_chart(fig4, use_container_width=True)
        st.caption("※ REINS Market Watch 実データ。東京都・戸建・土地は高価格帯が多いため階級区分が他県と異なります。")

    # --------------------------------------------------- 区単位(神奈川県のみ)
    if selected_pref == "神奈川県" and wards:
        st.markdown("---")
        st.markdown("**区単位で見る(REINS_Kanagawa 実成約データ)**")
        st.caption("神奈川県全体とは別集計・別階級です。")
        selected_ward = st.radio("区", list(wards.keys()), horizontal=True, key="ward_select")

        wd = wards.get(selected_ward, {}).get(category_p)
        if wd:
            ov = wd["overview"]
            wc1, wc2, wc3 = st.columns(3)
            wc1.metric(f"{selected_ward} 成約件数", f'{ov["count"]:,}件')
            wc2.metric(f"{selected_ward} 平均価格", f'{ov["avgPrice"]:,}万円' if ov.get("avgPrice") else "-")
            if ov.get("avgSqmPrice") is not None:
                wc3.metric(f"{selected_ward} 平均㎡単価", f'{ov["avgSqmPrice"]}万円')
            st.caption(f'集計期間: {ov.get("period", "")}')

            wbdf = pd.DataFrame(wd["priceBands"])
            fig5 = go.Figure(go.Bar(x=wbdf["pct"], y=wbdf["band"], orientation="h", marker_color=TEAL))
            fig5.update_layout(title=f"{selected_ward}・{category_p} 価格帯別分布", height=280, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig5, use_container_width=True)

            if wd.get("stationRanking"):
                sdf = pd.DataFrame(wd["stationRanking"])
                fig6 = go.Figure(go.Bar(x=sdf["avgPrice"], y=sdf["station"], orientation="h", marker_color=NAVY))
                fig6.update_layout(
                    title="最寄駅別 平均価格(万円)",
                    height=max(220, len(sdf) * 32), margin=dict(l=10, r=10, t=40, b=10),
                )
                st.plotly_chart(fig6, use_container_width=True)

            for key, label in [("ageBands", "築年代別統計"), ("tsuboBands", "坪単価帯別分布"), ("walkBands", "徒歩圏別相場")]:
                t = wd.get(key)
                if t:
                    st.markdown(f"**{selected_ward}・{category_p} {label}**")
                    st.dataframe(pd.DataFrame(t["rows"], columns=t["headers"]), use_container_width=True, hide_index=True)

# ---------------------------------------------------------------- 客層分析 tab
with tab_customer:
    if not demo or not migration or not wards:
        st.info("客層分析には demographics_config.json / reins_migration_data.json / reins_kanagawa_data.json が必要です。")
    else:
        cust_category = st.radio("カテゴリ", ["中古マンション", "戸建", "土地"], horizontal=True, key="cust_category")
        available_wards = [w for w in wards if w in migration]
        cust_ward = st.selectbox("区", available_wards, key="cust_ward")

        params = dict(demo["defaultParams"])
        with st.expander("試算の前提を調整"):
            c1, c2, c3, c4 = st.columns(4)
            params["repaymentRatio"] = c1.slider("返済負担率(%)", 15, 35, int(params["repaymentRatio"]))
            params["interestRate"] = c2.slider("金利(%)", 0.5, 4.0, float(params["interestRate"]), 0.01)
            params["downPaymentRatio"] = c3.slider("頭金充当率(%)", 0, 100, int(params["downPaymentRatio"]), 5)
            params["years"] = c4.slider("返済年数", 20, 35, int(params["years"]))

        age_rows = []
        for age in demo["ageProfiles"]:
            if age not in migration.get(cust_ward, {}).get("byAge", {}):
                continue
            r = calc_affordability.analyze(cust_ward, age, demo, migration, wards, params)
            age_rows.append(r)

        if age_rows:
            df = pd.DataFrame([{
                "年齢帯": r["age"], "転入超過数": r["netMigration"],
                "購入可能額(万円)": r["purchasePower"],
                f"{cust_category}届く割合(%)": r["affordableCoverage"].get(cust_category),
            } for r in age_rows])

            col1, col2 = st.columns(2)
            with col1:
                fig = go.Figure(go.Bar(
                    x=df["年齢帯"], y=df["転入超過数"],
                    marker_color=[TEAL if v >= 0 else CORAL for v in df["転入超過数"]],
                ))
                fig.update_layout(title=f"{cust_ward} 年齢帯別 転入超過数", height=300, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, use_container_width=True)
            with col2:
                fig2 = go.Figure(go.Bar(
                    x=df["年齢帯"], y=df[f"{cust_category}届く割合(%)"], marker_color=NAVY,
                ))
                fig2.update_layout(title=f"{cust_ward}・{cust_category} 届く物件割合(%)", height=300, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig2, use_container_width=True)

            st.dataframe(df, use_container_width=True, hide_index=True)

            # --- 全区横断の自動レポート ---
            st.markdown("---")
            st.markdown("**自動分析レポート(全区横断)**")
            report_rows = []
            for w in available_wards:
                for age in demo["ageProfiles"]:
                    if age not in migration.get(w, {}).get("byAge", {}):
                        continue
                    r = calc_affordability.analyze(w, age, demo, migration, wards, params)
                    report_rows.append({
                        "区": r["ward"], "年齢帯": r["age"],
                        "年収(万円)": r["income"], "貯蓄(万円)": r["savings"],
                        "購入可能額(万円)": r["purchasePower"], "転入超過数": r["netMigration"],
                        **{f"{c}届く割合(%)": r["affordableCoverage"].get(c) for c in ["中古マンション", "戸建", "土地"]},
                    })
            report_md = generate_report.build_report(report_rows, cust_category)
            st.markdown(report_md)
            st.download_button("レポートをMarkdownでダウンロード", data=report_md, file_name="customer_analysis_report.md", mime="text/markdown")
