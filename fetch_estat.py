#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_estat.py

e-Stat API (v3.0) から住民基本台帳ベースの人口統計を取得して reins_history.db に格納する。
対象調査: 「住民基本台帳に基づく人口、人口動態及び世帯数調査」(統計コード 00200241)

  discover   : 統計表を検索して statsDataId の候補を表示する（最初にこれで表IDを確認）
  age        : 年齢階級別人口   → population_by_age   (migration_by_age と同じ age_band 表記)
  dynamics   : 人口動態（出生・死亡・転入・転出等） → population_dynamics
  households : 世帯数・人口     → household_trends

事前準備:
  1. https://www.e-stat.go.jp/api/ で利用登録しアプリケーションIDを取得
  2. export ESTAT_APP_ID=<アプリケーションID>
  3. python3 create_api_tables.py でテーブル作成

Usage:
    # 1) 表IDを探す（例）
    python3 fetch_estat.py discover --search "市区町村別年齢階級別人口"
    python3 fetch_estat.py discover --search "市区町村別人口動態"
    python3 fetch_estat.py discover --search "市区町村別世帯数"

    # 2) 見つけた statsDataId で取得（対象区は api_targets.json の estat.area_codes）
    python3 fetch_estat.py age        --stats-data-id 00032XXXXX
    python3 fetch_estat.py dynamics   --stats-data-id 00032XXXXX
    python3 fetch_estat.py households --stats-data-id 00032XXXXX

同一 (年, 区, 指標) は INSERT OR REPLACE で上書きするので再実行しても重複しない。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"


def app_id():
    v = os.environ.get("ESTAT_APP_ID")
    if not v:
        sys.exit("環境変数 ESTAT_APP_ID が未設定です。https://www.e-stat.go.jp/api/ で登録してください。")
    return v


def as_list(x):
    """e-StatのJSONは要素が1件だとdict、複数だとlistになるので統一する"""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def get_json(path, params):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    r = requests.get(f"{BASE}/{path}", params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    root = next(iter(data.values()))
    status = root.get("RESULT", {}).get("STATUS")
    if status not in (0, "0"):
        sys.exit(f"e-Stat APIエラー (STATUS={status}): {root.get('RESULT', {}).get('ERROR_MSG')}")
    return root


# ── discover ─────────────────────────────────────────────────
def discover(args, cfg):
    root = get_json("getStatsList", {
        "appId": app_id(),
        "statsCode": args.stats_code or cfg["estat"]["stats_code"],
        "searchWord": args.search,
        "limit": args.limit,
    })
    tables = as_list(root.get("DATALIST_INF", {}).get("TABLE_INF"))
    if not tables:
        print("該当する統計表がありません（--search を外す、または語を変えて再試行してください）。")
        print("この統計コードの表がAPI非対応（ファイルダウンロードのみ）の可能性もあります。")
        return
    for t in tables:
        title = t.get("TITLE")
        if isinstance(title, dict):
            title = title.get("$")
        survey = t.get("SURVEY_DATE", "")
        print(f"{t.get('@id')}  [{survey}]  {t.get('STATISTICS_NAME','')} / {title}")
    print(f"\n{len(tables)}件。左端のIDを --stats-data-id に指定してください。")


# ── getStatsData 共通 ────────────────────────────────────────
def fetch_meta(stats_data_id):
    """getMetaInfoで分類定義だけを先に取得する。
    class_maps = {'cat01': {code: name}, 'area': {...}, 'time': {...}, ...}"""
    root = get_json("getMetaInfo", {"appId": app_id(), "statsDataId": stats_data_id})
    class_maps = {}
    for obj in as_list(root.get("METADATA_INF", {}).get("CLASS_INF", {}).get("CLASS_OBJ")):
        cid = obj.get("@id")
        class_maps[cid] = {c.get("@code"): c.get("@name") for c in as_list(obj.get("CLASS"))}
    return class_maps


def find_total_code(mapping):
    """'総数'/'総計'/'計'に相当する項目のコードを返す（余分な分類軸を固定するため）。
    完全一致（'総数'等そのもの）を優先し、なければ'国籍総数'のような部分一致にフォールバックする
    （'合計'等の紛らわしい語を拾わないよう、'総数'・'総計'のみ部分一致対象とする）。"""
    for code, name in mapping.items():
        if str(name) in ("総数", "総計", "計"):
            return code
    for code, name in mapping.items():
        if "総数" in str(name) or "総計" in str(name):
            return code
    return None


def fetch_stats_data(stats_data_id, area_codes, class_maps=None, extra_params=None):
    """全ページ取得して (class_maps, values) を返す。
    class_maps = {'cat01': {code: name}, 'area': {...}, 'time': {...}, ...}
    extra_params: {'cdCat01': '0', ...} のように余分な分類軸をコード固定するための追加パラメータ"""
    params_base = {
        "appId": app_id(),
        "statsDataId": stats_data_id,
        "cdArea": ",".join(area_codes),
        "metaGetFlg": "Y",
        "limit": 100000,
    }
    if extra_params:
        params_base.update(extra_params)
    out_values, class_maps = [], (class_maps or {})
    start = 1
    while True:
        root = get_json("getStatsData", {**params_base, "startPosition": start})
        sd = root.get("STATISTICAL_DATA", {})
        if not class_maps:
            for obj in as_list(sd.get("CLASS_INF", {}).get("CLASS_OBJ")):
                cid = obj.get("@id")
                class_maps[cid] = {c.get("@code"): c.get("@name")
                                   for c in as_list(obj.get("CLASS"))}
        out_values.extend(as_list(sd.get("DATA_INF", {}).get("VALUE")))
        nxt = sd.get("RESULT_INF", {}).get("NEXT_KEY")
        if not nxt:
            break
        start = int(nxt)
        time.sleep(0.5)
    return class_maps, out_values


def year_of(time_name, time_code):
    m = re.search(r"(\d{4})", str(time_name or ""))
    if m:
        return int(m.group(1))
    m = re.match(r"(\d{4})", str(time_code or ""))
    return int(m.group(1)) if m else None


def num(v):
    s = str(v).replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None  # "-" "X" "…" 等の秘匿・非該当


def ward_lookup(cfg):
    """e-Statのareaコード(先頭5桁で判定) → (区名, コード)"""
    m = {}
    for w in json.loads((HERE / "api_targets.json").read_text(encoding="utf-8"))["wards"]:
        m[w["code"]] = (w["name"], w["code"])
    return m


def resolve_area(area_code, area_name, wards):
    code5 = str(area_code)[:5]
    if code5 in wards:
        return wards[code5]
    # コードで引けない場合は名前末尾で照合（例: "横浜市港北区"）
    for name, code in wards.values():
        if str(area_name or "").endswith(name):
            return (name, code)
    return (None, None)


def gender_of(name):
    s = str(name or "")
    if s in ("計", "総数", "男女計") or ("男" in s and "女" in s):
        return "計"
    if "男" in s:
        return "男"
    if "女" in s:
        return "女"
    return None


AGE_TOTAL_WORDS = ("総数", "総計", "計")


def normalize_age_band(name):
    """'0～4歳'→'0-4'、'90～94歳'/'100歳以上'→'90+'、'総数'→'total'。年齢帯でなければNone"""
    s = str(name or "")
    if any(w == s for w in AGE_TOTAL_WORDS):
        return "total"
    m = re.match(r"(\d+)\s*[～~〜]\s*(\d+)歳", s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return "90+" if lo >= 90 else f"{lo}-{hi}"
    m = re.match(r"(\d+)歳以上", s)
    if m:
        return "90+" if int(m.group(1)) >= 90 else None
    return None


def find_class_id(class_maps, *keywords, exclude=("area", "time")):
    """分類名にキーワードを含むCLASS_OBJのidを探す（tab/cat01/cat02…のどれかは表による）"""
    for cid, mapping in class_maps.items():
        if cid in exclude:
            continue
        joined = "/".join(str(v) for v in mapping.values())
        if any(k in joined for k in keywords):
            return cid
    return None


def pin_other_axes(class_maps, keep_cids, exclude=("area", "time")):
    """keep_cids以外の分類軸（国籍・出生の月など）を'総数'コードに固定するcdCatNNパラメータを作る。
    固定できない（総数の無い）軸があれば、二重集計を避けるため表全体を諦めてNoneを返す。"""
    extra = {}
    for cid, mapping in class_maps.items():
        if cid in exclude or cid in keep_cids:
            continue
        total = find_total_code(mapping)
        if total is None:
            print(f"  警告: 分類軸 '{cid}' に総数がなく固定できません（{list(mapping.values())[:5]}...）")
            return None
        extra[f"cd{cid[0].upper()}{cid[1:]}"] = total  # cat01 → cdCat01, tab → cdTab
    return extra


# ── age: 年齢階級別人口 ──────────────────────────────────────
def cmd_age(args, cfg, conn):
    wards = ward_lookup(cfg)
    meta = fetch_meta(args.stats_data_id)
    age_cid = find_class_id(meta, "歳")
    gender_cid = find_class_id(meta, "男", "女")
    if not age_cid:
        sys.exit("この統計表には年齢階級の分類が見つかりません。discover で表を確認してください。")
    keep = {age_cid, "tab"} | ({gender_cid} if gender_cid else set())
    extra_params = pin_other_axes(meta, keep)
    if extra_params is None:
        sys.exit("余分な分類軸を安全に固定できないため中断しました（二重集計を防ぐため）。")
    class_maps, values = fetch_stats_data(args.stats_data_id, cfg["estat"]["area_codes"],
                                          class_maps=meta, extra_params=extra_params)

    # (year, ward, band, gender) で集計（90歳以上の複数帯を90+へ合算するため）
    acc = {}
    for v in values:
        ward, code = resolve_area(v.get("@area"), class_maps.get("area", {}).get(v.get("@area")), wards)
        if not ward:
            continue
        band = normalize_age_band(class_maps[age_cid].get(v.get(f"@{age_cid}")))
        if band is None:
            continue
        gender = gender_of(class_maps.get(gender_cid, {}).get(v.get(f"@{gender_cid}"))) if gender_cid else "計"
        if gender is None:
            continue
        year = year_of(class_maps.get("time", {}).get(v.get("@time")), v.get("@time"))
        val = num(v.get("$"))
        if year is None or val is None:
            continue
        k = (year, ward, code, band, gender)
        acc[k] = acc.get(k, 0) + val

    for (year, ward, code, band, gender), val in acc.items():
        conn.execute("""
            INSERT OR REPLACE INTO population_by_age
                (survey_year, ward, ward_code, age_band, gender, population, stats_data_id)
            VALUES (?,?,?,?,?,?,?)
        """, (year, ward, code, band, gender, int(val), args.stats_data_id))
    conn.commit()
    print(f"population_by_age: {len(acc)}行を登録 (statsDataId={args.stats_data_id})")


def bare_label(name):
    """'A7101_世帯数' → '世帯数'（社会・人口統計体系のコードプレフィックスを外す）。
    プレフィックスが無い表ではnameをそのまま返す。"""
    s = str(name or "")
    m = re.match(r"^[A-Z]\d+_(.+)$", s)
    return m.group(1) if m else s


# 人口動態として保存する指標（bare_labelの完全一致のみ。部分一致にすると
# 「世帯数」等の他カテゴリ指標を誤って拾ってしまうため使わない）
DYNAMICS_LABELS = {"出生数", "死亡数", "転入者数", "転出者数",
                   "転入者数（日本人移動者）", "転出者数（日本人移動者）"}


# ── dynamics: 人口動態 ───────────────────────────────────────
def cmd_dynamics(args, cfg, conn):
    wards = ward_lookup(cfg)
    ward_codes = cfg["estat"]["area_codes"]
    meta = fetch_meta(args.stats_data_id)
    ind_cid = find_class_id(meta, "出生", "死亡", "転入", "転出", "増減")
    if not ind_cid:
        sys.exit("この統計表には人口動態の指標分類が見つかりません。discover で表を確認してください。")
    extra_params = pin_other_axes(meta, {ind_cid, "tab"})
    if extra_params is None:
        sys.exit("余分な分類軸を安全に固定できないため中断しました（二重集計を防ぐため）。")
    class_maps, values = fetch_stats_data(args.stats_data_id, ward_codes,
                                          class_maps=meta, extra_params=extra_params)

    conn.execute(f"DELETE FROM population_dynamics WHERE ward_code IN ({','.join('?'*len(ward_codes))})",
                ward_codes)
    n = 0
    for v in values:
        ward, code = resolve_area(v.get("@area"), class_maps.get("area", {}).get(v.get("@area")), wards)
        if not ward:
            continue
        icode = v.get(f"@{ind_cid}")
        indicator = class_maps[ind_cid].get(icode)
        label = bare_label(indicator)
        if label not in DYNAMICS_LABELS:
            continue  # 総人口・世帯数などの他カテゴリ指標を除外（population_by_age/household_trends側で保持）
        year = year_of(class_maps.get("time", {}).get(v.get("@time")), v.get("@time"))
        val = num(v.get("$"))
        if year is None or val is None:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO population_dynamics
                (survey_year, ward, ward_code, indicator_code, indicator, value, unit, stats_data_id)
            VALUES (?,?,?,?,?,?,?,?)
        """, (year, ward, code, icode, label, val, v.get("@unit"), args.stats_data_id))
        n += 1
    conn.commit()
    print(f"population_dynamics: {n}行を登録 (statsDataId={args.stats_data_id})")


# ── households: 世帯数 ───────────────────────────────────────
def cmd_households(args, cfg, conn):
    wards = ward_lookup(cfg)
    ward_codes = cfg["estat"]["area_codes"]
    meta = fetch_meta(args.stats_data_id)
    item_cid = find_class_id(meta, "世帯")
    if not item_cid:
        sys.exit("この統計表には世帯数の分類が見つかりません。discover で表を確認してください。")
    extra_params = pin_other_axes(meta, {item_cid, "tab"})
    if extra_params is None:
        sys.exit("余分な分類軸を安全に固定できないため中断しました（二重集計を防ぐため）。")
    class_maps, values = fetch_stats_data(args.stats_data_id, ward_codes,
                                          class_maps=meta, extra_params=extra_params)

    hh, pop = {}, {}
    for v in values:
        ward, code = resolve_area(v.get("@area"), class_maps.get("area", {}).get(v.get("@area")), wards)
        if not ward:
            continue
        label = bare_label(class_maps[item_cid].get(v.get(f"@{item_cid}")))
        year = year_of(class_maps.get("time", {}).get(v.get("@time")), v.get("@time"))
        val = num(v.get("$"))
        if year is None or val is None:
            continue
        k = (year, ward, code)
        # 完全一致のみ（"一般世帯数"や"母子世帯数"等の細分類を誤って拾わないため）
        if label == "世帯数":
            hh[k] = val
        elif label in ("総人口", "住民基本台帳人口（総数）"):
            pop[k] = val

    conn.execute(f"DELETE FROM household_trends WHERE ward_code IN ({','.join('?'*len(ward_codes))})",
                ward_codes)
    for (year, ward, code), households in hh.items():
        population = pop.get((year, ward, code))
        avg = round(population / households, 2) if population and households else None
        conn.execute("""
            INSERT OR REPLACE INTO household_trends
                (survey_year, ward, ward_code, households, population, avg_household_size, stats_data_id)
            VALUES (?,?,?,?,?,?,?)
        """, (year, ward, code, int(households), int(population) if population else None,
              avg, args.stats_data_id))
    conn.commit()
    print(f"household_trends: {len(hh)}行を登録 (statsDataId={args.stats_data_id})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["discover", "age", "dynamics", "households"])
    ap.add_argument("--stats-data-id", help="e-Stat統計表ID（discoverで確認）")
    ap.add_argument("--search", default="", help="discover用の検索語")
    ap.add_argument("--stats-code", help="政府統計コード（既定: api_targets.jsonのestat.stats_code）")
    ap.add_argument("--limit", type=int, default=50, help="discoverの表示件数")
    ap.add_argument("--config", default=str(HERE / "api_targets.json"))
    ap.add_argument("--db", default=str(HERE / "reins_history.db"))
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))

    if args.command == "discover":
        discover(args, cfg)
        return

    if not args.stats_data_id:
        sys.exit("--stats-data-id を指定してください（fetch_estat.py discover で確認）。")

    conn = sqlite3.connect(args.db)
    {"age": cmd_age, "dynamics": cmd_dynamics, "households": cmd_households}[args.command](args, cfg, conn)
    conn.close()


if __name__ == "__main__":
    main()
