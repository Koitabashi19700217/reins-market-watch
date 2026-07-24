#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create_api_tables.py

reinfolib API（国交省 不動産情報ライブラリ）と e-Stat API から取得するデータ用の
テーブル・ビューを reins_history.db に追加する（冪等・既存9テーブルには触れない）。

追加テーブル:
  reinfolib_transactions   - 実際の取引価格情報 (XIT001)
  land_price_points        - 地価公示・地価調査ポイント (XPT002)
  city_planning_zones      - 都市計画決定GIS 用途地域 (XKT002)
  population_by_age        - 年齢階級別人口（e-Stat 住民基本台帳ベース）
  population_dynamics      - 人口動態：出生・死亡・転入・転出等（e-Stat）
  household_trends         - 世帯数推移（e-Stat）

追加ビュー（area_demand_supply_app.py 連携用）:
  v_reinfolib_price_bands  - reins_price_bands と同型（report_month/category/ward/band/pct）。
                             バンドラベルは calc_affordability.CATEGORY_BANDS と完全一致。
  v_land_price_summary     - 区×年の地価公示平均・中央値
  v_zone_mix               - 区ごとの用途地域構成比（面積ベース）

Usage:
    python3 create_api_tables.py [--db reins_history.db]
"""

import argparse
import sqlite3

SCHEMA = """
-- ── 1. 不動産取引価格情報 (reinfolib XIT001) ─────────────────
CREATE TABLE IF NOT EXISTS reinfolib_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_year INTEGER NOT NULL,          -- 取引時点の西暦年（リクエストのyear）
    trade_quarter INTEGER NOT NULL,       -- 四半期 1-4（リクエストのquarter）
    period TEXT,                          -- APIのPeriod（例: 2024年第3四半期）
    price_category TEXT,                  -- 不動産取引価格情報 / 成約価格情報
    type TEXT,                            -- APIのType（中古マンション等/宅地(土地と建物)/宅地(土地)/農地/林地）
    category TEXT,                        -- 既存アプリのカテゴリに正規化: 中古マンション/戸建/土地（対象外はNULL）
    region TEXT,                          -- 住宅地/商業地など
    prefecture TEXT,
    ward TEXT,                            -- Municipality（例: 横浜市港北区 → 港北区 に正規化）
    ward_code TEXT,                       -- 市区町村コード5桁（migration_by_age.ward_codeと同じ体系）
    district_name TEXT,                   -- 地区名（大字）
    trade_price INTEGER,                  -- 取引総額（円）
    price_manyen REAL,                    -- 取引総額（万円）… 価格帯集計用
    unit_price REAL,                      -- ㎡単価（円/㎡、土地のみ）
    floor_plan TEXT,                      -- 間取り
    area_sqm REAL,                        -- 面積（㎡）
    total_floor_area REAL,                -- 延床面積（㎡）
    building_year TEXT,                   -- 建築年（原文: "1995年" / "戦前" など）
    building_year_int INTEGER,            -- 建築年（数値化できた場合のみ）
    structure TEXT,                       -- 建物構造
    use TEXT,                             -- 用途
    purpose TEXT,                         -- 今後の利用目的
    city_planning TEXT,                   -- 都市計画（用途地域名）
    coverage_ratio REAL,                  -- 建蔽率(%)
    floor_area_ratio REAL,                -- 容積率(%)
    renovation TEXT,                      -- 改装
    remarks TEXT,                         -- 取引の事情等
    imported_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rtx_ward ON reinfolib_transactions(ward, category, trade_year);
CREATE INDEX IF NOT EXISTS idx_rtx_period ON reinfolib_transactions(trade_year, trade_quarter);

-- ── 2. 地価公示・地価調査ポイント (reinfolib XPT002) ─────────
CREATE TABLE IF NOT EXISTS land_price_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_year INTEGER NOT NULL,         -- 対象年
    point_id TEXT,                        -- 地点ID（API提供時）
    price_classification TEXT,            -- 地価公示 / 都道府県地価調査
    standard_lot_number TEXT,             -- 標準地/基準地番号（例: 港北-1）
    prefecture TEXT,
    ward TEXT,
    ward_code TEXT,
    address TEXT,                         -- 所在及び地番
    use_category TEXT,                    -- 住宅地/商業地など
    price_per_sqm INTEGER,                -- 当該年価格（円/㎡）
    yoy_change_rate REAL,                 -- 対前年変動率(%)
    zoning_use_area TEXT,                 -- 用途地域名
    coverage_ratio REAL,                  -- 建蔽率(%)
    floor_area_ratio REAL,                -- 容積率(%)
    nearest_station TEXT,
    lat REAL,
    lon REAL,
    raw_json TEXT,                        -- APIプロパティ全文（フィールド名変更に備えた保険）
    imported_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lpp_ward ON land_price_points(ward, target_year);

-- ── 3. 都市計画 用途地域 (reinfolib XKT002) ──────────────────
-- 注意: ベクトルタイル由来のためポリゴンはタイル境界で分割されている。
--       面積(area_sqm)の合計は区単位で概ね正しいが、行数=地域数ではない。
CREATE TABLE IF NOT EXISTS city_planning_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ward TEXT,
    ward_code TEXT,
    prefecture TEXT,
    youto_id TEXT,                        -- 用途地域分類コード
    use_area TEXT,                        -- 用途地域名（第一種低層住居専用地域 など）
    use_group TEXT,                       -- 住居系/商業系/工業系 に正規化
    building_coverage_ratio REAL,         -- 建蔽率(%)
    floor_area_ratio REAL,                -- 容積率(%)
    decision_date TEXT,
    area_sqm REAL,                        -- ポリゴン面積（近似計算、㎡）
    centroid_lat REAL,
    centroid_lon REAL,
    tile TEXT,                            -- 取得元タイル z/x/y
    raw_json TEXT,
    imported_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cpz_ward ON city_planning_zones(ward, use_group);

-- ── 4. 年齢階級別人口 (e-Stat 住民基本台帳ベース) ─────────────
-- migration_by_age（フロー: 転入超過）に対するストック側。age_bandは同じ表記（"30-34"等）。
CREATE TABLE IF NOT EXISTS population_by_age (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_year INTEGER NOT NULL,
    ward TEXT NOT NULL,
    ward_code TEXT,
    age_band TEXT NOT NULL,               -- "0-4"〜"85-89","90+","total"
    gender TEXT NOT NULL DEFAULT '計',    -- 計/男/女
    population INTEGER,
    stats_data_id TEXT,                   -- 出典のe-Stat統計表ID
    imported_at TEXT DEFAULT (datetime('now')),
    UNIQUE(survey_year, ward, age_band, gender)
);
CREATE INDEX IF NOT EXISTS idx_pba_ward_year ON population_by_age(ward, survey_year);

-- ── 5. 人口動態 (e-Stat 住民基本台帳ベース) ──────────────────
-- 縦持ち: indicator = 出生者数/死亡者数/転入者数/転出者数/自然増減数/社会増減数 など
CREATE TABLE IF NOT EXISTS population_dynamics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_year INTEGER NOT NULL,
    ward TEXT NOT NULL,
    ward_code TEXT,
    indicator_code TEXT,                  -- e-Statの分類コード
    indicator TEXT NOT NULL,              -- 分類名
    value REAL,
    unit TEXT,
    stats_data_id TEXT,
    imported_at TEXT DEFAULT (datetime('now')),
    UNIQUE(survey_year, ward, indicator)
);
CREATE INDEX IF NOT EXISTS idx_pd_ward_year ON population_dynamics(ward, survey_year);

-- ── 6. 世帯数推移 (e-Stat 住民基本台帳ベース) ────────────────
CREATE TABLE IF NOT EXISTS household_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_year INTEGER NOT NULL,
    ward TEXT NOT NULL,
    ward_code TEXT,
    households INTEGER,                   -- 世帯数
    population INTEGER,                   -- 総人口（同一表から取れた場合）
    avg_household_size REAL,              -- 1世帯あたり人員
    stats_data_id TEXT,
    imported_at TEXT DEFAULT (datetime('now')),
    UNIQUE(survey_year, ward)
);
"""

# バンドラベルは calc_affordability.py の CATEGORY_BANDS と一致させること
VIEWS = """
DROP VIEW IF EXISTS v_reinfolib_price_bands;
CREATE VIEW v_reinfolib_price_bands AS
WITH banded AS (
    SELECT
        trade_year || '-Q' || trade_quarter AS report_quarter,
        category, ward,
        CASE
          WHEN category = '中古マンション' THEN
            CASE
              WHEN price_manyen <= 1000 THEN '~1,000万'
              WHEN price_manyen <= 2000 THEN '1,000~2,000万'
              WHEN price_manyen <= 3000 THEN '2,000~3,000万'
              WHEN price_manyen <= 4000 THEN '3,000~4,000万'
              WHEN price_manyen <= 5000 THEN '4,000~5,000万'
              WHEN price_manyen <= 6000 THEN '5,000~6,000万'
              WHEN price_manyen <= 8000 THEN '6,000~8,000万'
              WHEN price_manyen <= 10000 THEN '8,000~1億'
              ELSE '1億超' END
          WHEN category = '戸建' THEN
            CASE
              WHEN price_manyen <= 2000 THEN '〜2,000万'
              WHEN price_manyen <= 3000 THEN '2,000〜3,000万'
              WHEN price_manyen <= 4000 THEN '3,000〜4,000万'
              WHEN price_manyen <= 5000 THEN '4,000〜5,000万'
              WHEN price_manyen <= 6000 THEN '5,000〜6,000万'
              WHEN price_manyen <= 7000 THEN '6,000〜7,000万'
              WHEN price_manyen <= 8000 THEN '7,000〜8,000万'
              WHEN price_manyen <= 10000 THEN '8,000〜1億'
              ELSE '1億超' END
          WHEN category = '土地' THEN
            CASE
              WHEN price_manyen <= 2000 THEN '〜2,000万'
              WHEN price_manyen <= 3000 THEN '2,000〜3,000万'
              WHEN price_manyen <= 5000 THEN '3,000〜5,000万'
              WHEN price_manyen <= 8000 THEN '5,000〜8,000万'
              WHEN price_manyen <= 12000 THEN '8,000〜1.2億'
              WHEN price_manyen <= 20000 THEN '1.2億〜2億'
              ELSE '2億超' END
        END AS band
    FROM reinfolib_transactions
    WHERE category IS NOT NULL AND price_manyen IS NOT NULL
)
SELECT report_quarter, category, ward, band,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY report_quarter, category, ward), 1) AS pct,
       COUNT(*) AS n
FROM banded
GROUP BY report_quarter, category, ward, band;

DROP VIEW IF EXISTS v_land_price_summary;
CREATE VIEW v_land_price_summary AS
SELECT target_year, ward, use_category, price_classification,
       COUNT(*) AS points,
       ROUND(AVG(price_per_sqm)) AS avg_price_sqm,
       ROUND(AVG(yoy_change_rate), 2) AS avg_yoy_pct
FROM land_price_points
GROUP BY target_year, ward, use_category, price_classification;

DROP VIEW IF EXISTS v_zone_mix;
CREATE VIEW v_zone_mix AS
SELECT ward, use_group, use_area,
       ROUND(SUM(area_sqm)) AS area_sqm,
       ROUND(100.0 * SUM(area_sqm) / SUM(SUM(area_sqm)) OVER (PARTITION BY ward), 1) AS pct_of_ward,
       ROUND(AVG(floor_area_ratio)) AS avg_floor_area_ratio,
       ROUND(AVG(building_coverage_ratio)) AS avg_coverage_ratio
FROM city_planning_zones
WHERE area_sqm IS NOT NULL
GROUP BY ward, use_group, use_area;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="reins_history.db")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.executescript(VIEWS)
    conn.commit()

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name")]
    print(f"OK: {args.db}")
    for t in tables:
        print(" ", t)
    conn.close()


if __name__ == "__main__":
    main()
