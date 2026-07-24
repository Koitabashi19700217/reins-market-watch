#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_reinfolib.py

国交省 不動産情報ライブラリAPI から取得して reins_history.db に格納する。

  transactions : XIT001 不動産価格（取引価格・成約価格）情報 → reinfolib_transactions
  landprice    : XPT002 地価公示・地価調査ポイント           → land_price_points
  cityplanning : XKT002 都市計画決定GIS 用途地域             → city_planning_zones
  all          : 上記すべて

事前準備:
  1. https://www.reinfolib.mlit.go.jp/api/request/ でAPI利用申請しキーを取得
  2. export REINFOLIB_API_KEY=<発行されたキー>
  3. python3 create_api_tables.py でテーブル作成

Usage:
    python3 fetch_reinfolib.py transactions [--config api_targets.json] [--db reins_history.db]
    python3 fetch_reinfolib.py landprice
    python3 fetch_reinfolib.py cityplanning
    python3 fetch_reinfolib.py all

再実行時は対象範囲（区×期間）の既存行を削除してから入れ直すので冪等。
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
BASE = "https://www.reinfolib.mlit.go.jp/ex-api/external"
SLEEP_SEC = 1.2  # 連続リクエストの間隔（サーバー負荷への配慮）

# XIT001のType → 既存アプリのカテゴリ
TYPE_TO_CATEGORY = {
    "中古マンション等": "中古マンション",
    "宅地(土地と建物)": "戸建",
    "宅地(土地)": "土地",
}

# 用途地域名 → 住居系/商業系/工業系
def use_group(name):
    if not name:
        return None
    if "住居" in name or "田園" in name:
        return "住居系"
    if "商業" in name:
        return "商業系"
    if "工業" in name:
        return "工業系"
    return "その他"


def api_key():
    key = os.environ.get("REINFOLIB_API_KEY")
    if not key:
        sys.exit("環境変数 REINFOLIB_API_KEY が未設定です。"
                 "https://www.reinfolib.mlit.go.jp/api/request/ で申請してください。")
    return key


def get(path, params, key, retries=3):
    url = f"{BASE}/{path}"
    for i in range(retries):
        r = requests.get(url, params=params,
                         headers={"Ocp-Apim-Subscription-Key": key}, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:  # 該当データなしのタイルで返ることがある
            return None
        if r.status_code == 429:
            time.sleep(10 * (i + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"{path}: リトライ上限に到達")


def to_float(v):
    if v is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def normalize_ward(municipality, wards):
    """'横浜市港北区' → '港北区'（既存テーブルの区名表記に合わせる）"""
    if not municipality:
        return None
    for w in wards:
        if municipality.endswith(w["name"]):
            return w["name"]
    return municipality


# ── タイル座標（XYZ方式）─────────────────────────────────────
def deg2tile(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def tiles_for_bbox(bbox, z):
    lon_min, lat_min, lon_max, lat_max = bbox
    x0, y0 = deg2tile(lat_max, lon_min, z)  # 北西
    x1, y1 = deg2tile(lat_min, lon_max, z)  # 南東
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def polygon_area_sqm(coords):
    """経緯度リング([[lon,lat],...])の面積を㎡で近似（シューレース + 緯度補正）"""
    if len(coords) < 3:
        return 0.0
    lat0 = math.radians(sum(c[1] for c in coords) / len(coords))
    mx = 111320.0 * math.cos(lat0)  # 経度1度あたりm
    my = 110540.0                   # 緯度1度あたりm
    s = 0.0
    for i in range(len(coords) - 1):
        x1, y1 = coords[i][0] * mx, coords[i][1] * my
        x2, y2 = coords[i + 1][0] * mx, coords[i + 1][1] * my
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def geometry_area_centroid(geom):
    gtype, coords = geom.get("type"), geom.get("coordinates", [])
    polys = []
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    else:
        return None, None, None
    total = 0.0
    pts = []
    for poly in polys:
        if not poly:
            continue
        total += polygon_area_sqm(poly[0])          # 外周
        for hole in poly[1:]:
            total -= polygon_area_sqm(hole)         # 穴
        pts.extend(poly[0])
    if not pts:
        return None, None, None
    lat = sum(p[1] for p in pts) / len(pts)
    lon = sum(p[0] for p in pts) / len(pts)
    return total, lat, lon


def pick(props, *keys):
    """プロパティ名の表記ゆれに備えて候補キーを順に試す"""
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return props[k]
    return None


# ── 1. 取引価格情報 XIT001 ──────────────────────────────────
def fetch_transactions(conn, cfg, key):
    tcfg = cfg["transactions"]
    total = 0
    for ward in cfg["wards"]:
        for year in range(tcfg["year_from"], tcfg["year_to"] + 1):
            for quarter in (1, 2, 3, 4):
                params = {"year": year, "quarter": quarter, "city": ward["code"]}
                if tcfg.get("price_classification"):
                    params["priceClassification"] = tcfg["price_classification"]
                data = get("XIT001", params, key)
                time.sleep(SLEEP_SEC)
                rows = (data or {}).get("data", [])
                conn.execute(
                    "DELETE FROM reinfolib_transactions WHERE ward_code=? AND trade_year=? AND trade_quarter=?",
                    (ward["code"], year, quarter))
                for d in rows:
                    trade_price = to_int(d.get("TradePrice"))
                    conn.execute("""
                        INSERT INTO reinfolib_transactions (
                            trade_year, trade_quarter, period, price_category, type, category,
                            region, prefecture, ward, ward_code, district_name,
                            trade_price, price_manyen, unit_price, floor_plan, area_sqm,
                            total_floor_area, building_year, building_year_int, structure,
                            use, purpose, city_planning, coverage_ratio, floor_area_ratio,
                            renovation, remarks)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        year, quarter, d.get("Period"), d.get("PriceCategory"),
                        d.get("Type"), TYPE_TO_CATEGORY.get(d.get("Type")),
                        d.get("Region"), d.get("Prefecture"),
                        normalize_ward(d.get("Municipality"), cfg["wards"]), ward["code"],
                        d.get("DistrictName"),
                        trade_price,
                        round(trade_price / 10000, 1) if trade_price else None,
                        to_float(d.get("UnitPrice")), d.get("FloorPlan"),
                        to_float(d.get("Area")), to_float(d.get("TotalFloorArea")),
                        d.get("BuildingYear"), to_int(d.get("BuildingYear")),
                        d.get("Structure"), d.get("Use"), d.get("Purpose"),
                        d.get("CityPlanning"), to_float(d.get("CoverageRatio")),
                        to_float(d.get("FloorAreaRatio")), d.get("Renovation"),
                        d.get("Remarks"),
                    ))
                conn.commit()
                total += len(rows)
                print(f"  XIT001 {ward['name']} {year}Q{quarter}: {len(rows)}件")
    print(f"transactions 完了: {total}件")


# ── 2. 地価公示・地価調査 XPT002 ────────────────────────────
def fetch_landprice(conn, cfg, key):
    lcfg = cfg["land_price"]
    z = lcfg.get("zoom", 13)
    total = 0
    for ward in cfg["wards"]:
        tiles = tiles_for_bbox(ward["bbox"], z)
        for year in range(lcfg["year_from"], lcfg["year_to"] + 1):
            conn.execute("DELETE FROM land_price_points WHERE ward_code=? AND target_year=?",
                         (ward["code"], year))
            seen = set()
            for (tz, tx, ty) in tiles:
                data = get("XPT002", {
                    "response_format": "geojson", "z": tz, "x": tx, "y": ty, "year": year,
                }, key)
                time.sleep(SLEEP_SEC)
                for f in (data or {}).get("features", []):
                    p = f.get("properties", {})
                    code = str(pick(p, "city_code", "citycode") or "")
                    if code != ward["code"]:
                        continue  # bboxのはみ出し分を除外
                    geom = f.get("geometry", {})
                    coords = geom.get("coordinates", [None, None])
                    lon, lat = (coords + [None, None])[:2] if isinstance(coords, list) else (None, None)
                    lot = pick(p, "standard_lot_number_ja", "standard_lot_number")
                    dedup = (lot or pick(p, "point_id"), pick(p, "land_price_type"), lat, lon)
                    if dedup in seen:  # 隣接タイルの重複
                        continue
                    seen.add(dedup)
                    cls = pick(p, "land_price_type", "price_classification")
                    cls_label = {0: "地価公示", "0": "地価公示",
                                 1: "都道府県地価調査", "1": "都道府県地価調査"}.get(cls, str(cls))
                    conn.execute("""
                        INSERT INTO land_price_points (
                            target_year, point_id, price_classification, standard_lot_number,
                            prefecture, ward, ward_code, address, use_category,
                            price_per_sqm, yoy_change_rate, zoning_use_area,
                            coverage_ratio, floor_area_ratio, nearest_station,
                            lat, lon, raw_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        year, pick(p, "point_id"), cls_label, lot,
                        pick(p, "prefecture_name_ja", "prefecture"),
                        ward["name"], ward["code"],
                        pick(p, "place_name_ja", "location_number_ja", "residence_display_name_ja"),
                        pick(p, "use_category_name_ja", "land_use_ja", "usage_ja"),
                        to_int(pick(p, "u_current_years_price_ja", "current_years_price_ja",
                                    "posted_land_price", "price")),
                        to_float(pick(p, "year_on_year_change_rate",
                                      "u_year_on_year_change_rate_ja")),
                        pick(p, "regulations_use_category_name_ja", "u_regulations_use_category_name_ja", "use_area_ja"),
                        to_float(pick(p, "u_regulations_building_coverage_ratio_ja")),
                        to_float(pick(p, "u_regulations_floor_area_ratio_ja")),
                        pick(p, "nearest_station_name_ja",
                             "u_road_distance_to_nearest_station_name_ja"),
                        lat, lon, json.dumps(p, ensure_ascii=False),
                    ))
                    total += 1
            conn.commit()
            print(f"  XPT002 {ward['name']} {year}: 累計{total}地点")
    print(f"landprice 完了: {total}地点")


# ── 3. 都市計画 用途地域 XKT002 ─────────────────────────────
# 注意: このAPIのcity_codeは政令指定都市だと市レベル(横浜市=14100)までしか
#       返らず、区レベル(港北区=14109等)には分解されない。そのためXIT001/XPT002
#       と違って city_code では区を判定できず、ポリゴン重心とward bboxの
#       突き合わせで区を推定する（bboxは隣接区と重なりうるので、重なった場合は
#       bbox中心に近い区を採用）。行政区境界そのものではないため境界付近は近似。
def bbox_contains(bbox, lat, lon):
    lon_min, lat_min, lon_max, lat_max = bbox
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


def bbox_center(bbox):
    lon_min, lat_min, lon_max, lat_max = bbox
    return (lat_min + lat_max) / 2, (lon_min + lon_max) / 2


def assign_ward(clat, clon, wards):
    candidates = [w for w in wards if bbox_contains(w["bbox"], clat, clon)]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    def dist(w):
        c_lat, c_lon = bbox_center(w["bbox"])
        return (c_lat - clat) ** 2 + (c_lon - clon) ** 2
    return min(candidates, key=dist)


def fetch_cityplanning(conn, cfg, key):
    z = cfg["city_planning"].get("zoom", 13)
    wards = cfg["wards"]
    target_city_names = {"横浜市"}  # ward["name"]は区名なのでcity_nameで市を絞る

    for ward in wards:
        conn.execute("DELETE FROM city_planning_zones WHERE ward_code=?", (ward["code"],))

    tile_set = sorted(set().union(*(tiles_for_bbox(w["bbox"], z) for w in wards)))
    total, skipped = 0, 0
    for (tz, tx, ty) in tile_set:
        data = get("XKT002", {
            "response_format": "geojson", "z": tz, "x": tx, "y": ty,
        }, key)
        time.sleep(SLEEP_SEC)
        for f in (data or {}).get("features", []):
            p = f.get("properties", {})
            if pick(p, "city_name") not in target_city_names:
                continue
            area, clat, clon = geometry_area_centroid(f.get("geometry", {}))
            if clat is None:
                continue
            ward = assign_ward(clat, clon, wards)
            if not ward:
                skipped += 1  # 横浜市内だが対象4区の外
                continue
            name = pick(p, "use_area_ja", "youto_area_ja", "use_area")
            conn.execute("""
                INSERT INTO city_planning_zones (
                    ward, ward_code, prefecture, youto_id, use_area, use_group,
                    building_coverage_ratio, floor_area_ratio, decision_date,
                    area_sqm, centroid_lat, centroid_lon, tile, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                ward["name"], ward["code"], pick(p, "prefecture"),
                str(pick(p, "youto_id") or ""), name, use_group(name),
                to_float(pick(p, "u_building_coverage_ratio_ja")),
                to_float(pick(p, "u_floor_area_ratio_ja")),
                pick(p, "decision_date"),
                area, clat, clon, f"{tz}/{tx}/{ty}",
                json.dumps(p, ensure_ascii=False),
            ))
            total += 1
        conn.commit()
    print(f"cityplanning 完了: {total}ポリゴン（対象4区外のためスキップ: {skipped}）")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["transactions", "landprice", "cityplanning", "all"])
    ap.add_argument("--config", default=str(HERE / "api_targets.json"))
    ap.add_argument("--db", default=str(HERE / "reins_history.db"))
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    key = api_key()
    conn = sqlite3.connect(args.db)

    if args.command in ("transactions", "all"):
        fetch_transactions(conn, cfg, key)
    if args.command in ("landprice", "all"):
        fetch_landprice(conn, cfg, key)
    if args.command in ("cityplanning", "all"):
        fetch_cityplanning(conn, cfg, key)
    conn.close()


if __name__ == "__main__":
    main()
