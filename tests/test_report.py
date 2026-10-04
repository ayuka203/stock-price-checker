import json
from datetime import date

import pytest

import report


def theme(name, daily, weekly=None, n=3, layer="watchlist"):
    return {"name": name, "layer": layer, "daily_pct": daily, "weekly_pct": weekly, "n": n}


# ---------------------------------------------------------------- headline

def test_headline_up_and_down():
    themes = [theme("送配電・計測", 0.052), theme("原子力燃料・関連素材", -0.031), theme("その他", 0.01)]
    assert report.make_headline(themes, "daily") == (
        "送配電・計測が上昇（平均+5.2%）、原子力燃料・関連素材は下落（平均-3.1%）"
    )


def test_headline_quiet_daily_boundary():
    assert report.make_headline([theme("A", 0.0099), theme("B", -0.005)], "daily") == "エネルギー関連は小動き"
    # ちょうど 1% は小動きではない
    assert "小動き" not in report.make_headline([theme("A", 0.01), theme("B", -0.005)], "daily")


def test_headline_quiet_weekly_boundary_and_prefix():
    quiet = [theme("A", 0.5, 0.0199), theme("B", 0.5, -0.01)]
    assert report.make_headline(quiet, "weekly") == "先週はエネルギー関連は小動き"
    loud = [theme("A", 0.0, 0.02), theme("B", 0.0, -0.01)]
    assert report.make_headline(loud, "weekly") == "先週はAが上昇（平均+2.0%）、Bは下落（平均-1.0%）"


def test_headline_weekly_uses_weekly_not_daily():
    themes = [theme("A", 0.0, 0.05), theme("B", 0.0, -0.04)]
    assert report.make_headline(themes, "daily") == "エネルギー関連は小動き"
    assert report.make_headline(themes, "weekly").startswith("先週はAが上昇")


def test_headline_all_up_and_all_down():
    up = report.make_headline([theme("A", 0.03), theme("B", 0.02)], "daily")
    assert "全面高" in up and "A" in up
    down = report.make_headline([theme("A", -0.03), theme("B", -0.02)], "daily")
    assert "全面安" in down and "A" in down


def test_headline_tie_break_by_theme_name():
    themes = [theme("B", 0.05), theme("A", 0.05), theme("D", -0.05), theme("C", -0.05)]
    assert report.make_headline(themes, "daily") == "Aが上昇（平均+5.0%）、Cは下落（平均-5.0%）"


def test_headline_ignores_none_and_handles_empty():
    assert report.make_headline([theme("A", None), theme("B", 0.05), theme("C", -0.05)], "daily") == (
        "Bが上昇（平均+5.0%）、Cは下落（平均-5.0%）"
    )
    assert report.make_headline([], "daily") == "株価データなし"
    assert report.make_headline([theme("A", None)], "daily") == "株価データなし"


# ---------------------------------------------------------------- edition / market

def test_edition_monday_is_weekly():
    assert report.edition_of(date(2026, 10, 5)) == "weekly"  # 月
    for day in (6, 7, 8, 9, 10, 11):
        assert report.edition_of(date(2026, 10, day)) == "daily"


def test_market_of():
    assert report.market_of("7011.T") == "JP"
    assert report.market_of("OKLO") == "US"
    assert report.market_of("URA") == "US"


# ---------------------------------------------------------------- stale

def test_stale_first_run_is_false():
    stale, state = report.resolve_stale({"JP": "2026-10-02", "US": "2026-10-02"}, None, "2026-10-05")
    assert stale == {"JP_stale": False, "US_stale": False}
    assert state["asof"] == {"JP": "2026-10-02", "US": "2026-10-02"}


def test_stale_when_asof_not_advanced():
    prev = {"report_date": "2026-10-05", "asof": {"JP": "2026-10-02", "US": "2026-10-02"}, "prev_asof": {}}
    stale, state = report.resolve_stale({"JP": "2026-10-03", "US": "2026-10-02"}, prev, "2026-10-06")
    assert stale == {"JP_stale": False, "US_stale": True}
    assert state["report_date"] == "2026-10-06"
    assert state["prev_asof"] == {"JP": "2026-10-02", "US": "2026-10-02"}


def test_stale_when_asof_none_after_previous_value():
    prev = {"report_date": "2026-10-05", "asof": {"JP": "2026-10-02", "US": None}, "prev_asof": {}}
    stale, state = report.resolve_stale({"JP": None, "US": None}, prev, "2026-10-06")
    assert stale == {"JP_stale": True, "US_stale": False}
    assert state["asof"]["JP"] == "2026-10-02"  # 失われない


def test_stale_same_day_rerun_compares_to_first_run_baseline():
    asof = {"JP": "2026-10-03", "US": "2026-10-03"}
    first_stale, first_state = report.resolve_stale(
        asof, {"report_date": "2026-10-05", "asof": {"JP": "2026-10-02", "US": "2026-10-02"}, "prev_asof": {}},
        "2026-10-06",
    )
    assert first_stale == {"JP_stale": False, "US_stale": False}
    again_stale, again_state = report.resolve_stale(asof, first_state, "2026-10-06")
    assert again_stale == first_stale
    assert again_state["prev_asof"] == first_state["prev_asof"]


# ---------------------------------------------------------------- build_report

WATCH = {
    "themes": [
        {"name": "W1", "tickers": [
            {"symbol": "1000.T", "label": "JP一", "keywords": ["日一"]},
            {"symbol": "AAA", "label": "US一", "keywords": ["Aaa"]},
        ]},
    ]
}
UNIVERSE = {
    "themes": [
        {"name": "U1", "tickers": [
            {"symbol": "AAA", "label": "US一(重複)", "keywords": ["dup"]},
            {"symbol": "BBB", "label": "US二", "keywords": ["Bbb"]},
            {"symbol": "2000.T", "label": "JP二"},
        ]},
        {"name": "U2", "tickers": [{"symbol": "AAA", "label": "dup"}]},
    ]
}
MACRO = {"macro": [{"symbol": "CL=F", "label": "WTI原油"}, {"symbol": "^N225", "label": "日経平均"}]}


def metric(date_, close=100.0, daily=0.01, weekly=0.02, z=0.5, wz=0.4, flags=None):
    return {"date": date_, "close": close, "daily_pct": daily, "weekly_pct": weekly,
            "z": z, "weekly_z": wz, "flags": flags or []}


def build(metrics=None, report_date=date(2026, 10, 6), state=None, failures=()):
    if metrics is None:
        metrics = {
            "1000.T": metric("2026-10-05", daily=0.04, weekly=0.1, z=3.1, wz=2.5, flags=["52w_high"]),
            "AAA": metric("2026-10-03", daily=-0.02, weekly=0.01, z=-2.4, wz=0.3),
            "BBB": metric("2026-10-02", daily=None, weekly=None, z=None, wz=None),
            "CL=F": metric("2026-10-06", close=91.11, daily=-0.004, weekly=0.02, z=None, wz=None),
        }
    return report.build_report(report_date, WATCH, UNIVERSE, MACRO, metrics, list(failures), state)


def test_top_level_schema_keys_and_types():
    r, _ = build()
    assert list(r.keys()) == [
        "schema_version", "report_date", "edition", "asof", "headline",
        "themes", "movers", "macro", "tickers", "fetch_failures",
    ]
    assert r["schema_version"] == 1
    assert r["report_date"] == "2026-10-06"
    assert r["edition"] == "daily"
    assert list(r["asof"].keys()) == ["JP", "US", "JP_stale", "US_stale"]
    assert isinstance(r["headline"], str)
    for key in ("themes", "movers", "macro", "tickers", "fetch_failures"):
        assert isinstance(r[key], list)


def test_element_schemas():
    r, _ = build()
    assert list(r["themes"][0].keys()) == ["name", "layer", "daily_pct", "weekly_pct", "n"]
    assert list(r["movers"][0].keys()) == [
        "symbol", "label", "theme", "close", "daily_pct", "weekly_pct", "z", "flags", "keywords",
    ]
    assert list(r["macro"][0].keys()) == ["symbol", "label", "close", "daily_pct", "weekly_pct", "asof"]
    assert list(r["tickers"][0].keys()) == [
        "symbol", "label", "theme", "layer", "close", "daily_pct", "weekly_pct", "z", "flags", "asof", "stale",
    ]
    t = r["tickers"][0]
    assert isinstance(t["flags"], list) and isinstance(t["stale"], bool)
    assert isinstance(t["close"], float) and isinstance(t["asof"], str)
    assert isinstance(r["themes"][0]["n"], int)


def test_json_roundtrip_keeps_japanese_unescaped():
    r, _ = build()
    text = json.dumps(r, ensure_ascii=False)
    assert "JP一" in text
    assert json.loads(text) == r


def test_none_values_serialise_as_null():
    r, _ = build()
    bbb = next(t for t in r["tickers"] if t["symbol"] == "BBB")
    assert bbb["daily_pct"] is None and bbb["z"] is None
    assert '"daily_pct": null' in json.dumps(r)


def test_asof_per_market_excludes_macro_and_takes_max():
    r, _ = build()
    # JP: 1000.T=10-05, 2000.T 無し。US: AAA=10-03, BBB=10-02 → 最大 10-03。CL=F(10-06) は含めない
    assert r["asof"]["JP"] == "2026-10-05"
    assert r["asof"]["US"] == "2026-10-03"
    assert r["macro"][0]["asof"] == "2026-10-06"
    assert r["macro"][1]["asof"] is None  # データ無し


def test_ticker_stale_and_order_and_dedup():
    r, _ = build()
    assert [t["symbol"] for t in r["tickers"]] == ["1000.T", "AAA", "BBB", "2000.T"]
    by = {t["symbol"]: t for t in r["tickers"]}
    assert by["AAA"]["layer"] == "watchlist" and by["AAA"]["theme"] == "W1" and by["AAA"]["label"] == "US一"
    assert by["BBB"]["stale"] is True  # 10-02 < US asof 10-03
    assert by["AAA"]["stale"] is False
    assert by["1000.T"]["stale"] is False


def test_ticker_without_data_is_stale_when_market_has_asof():
    r, _ = build()
    by = {t["symbol"]: t for t in r["tickers"]}
    # 2000.T は JP に日付のある銘柄(1000.T)があるので stale
    assert by["2000.T"]["stale"] is True
    assert by["2000.T"]["asof"] is None and by["2000.T"]["close"] is None


def test_themes_average_dedup_and_n():
    r, _ = build()
    themes = {t["name"]: t for t in r["themes"]}
    # W1: 1000.T(0.04) と AAA(-0.02) の平均
    assert themes["W1"]["daily_pct"] == pytest.approx(0.01)
    assert themes["W1"]["n"] == 2
    # U1: AAA は watchlist 側のため数えない。BBB(None)、2000.T(データ無し) → 平均なし
    assert themes["U1"]["daily_pct"] is None and themes["U1"]["n"] == 0
    # U2: AAA は watchlist で既出 → 空
    assert themes["U2"]["n"] == 0
    assert [t["layer"] for t in r["themes"]] == ["watchlist", "universe", "universe"]


def test_theme_average_excludes_none_per_field():
    metrics = {
        "1000.T": metric("2026-10-05", daily=0.04, weekly=None),
        "AAA": metric("2026-10-05", daily=0.02, weekly=0.10),
    }
    r, _ = build(metrics)
    w1 = r["themes"][0]
    assert w1["daily_pct"] == pytest.approx(0.03)
    assert w1["weekly_pct"] == pytest.approx(0.10)
    assert w1["n"] == 2


def test_movers_daily_threshold_order_and_keywords():
    r, _ = build()
    assert [m["symbol"] for m in r["movers"]] == ["1000.T", "AAA"]  # |3.1| > |-2.4|
    assert r["movers"][0]["z"] == 3.1
    assert r["movers"][0]["flags"] == ["52w_high"]
    assert r["movers"][0]["keywords"] == ["日一"]
    assert r["movers"][1]["keywords"] == ["Aaa"]  # watchlist 側の keywords


def test_movers_boundary_and_empty():
    metrics = {"1000.T": metric("2026-10-05", z=1.99), "AAA": metric("2026-10-05", z=-1.99)}
    r, _ = build(metrics)
    assert r["movers"] == []
    metrics = {"1000.T": metric("2026-10-05", z=2.0)}
    r, _ = build(metrics)
    assert [m["symbol"] for m in r["movers"]] == ["1000.T"]
    r, _ = build({})
    assert r["movers"] == []


def test_movers_max_three_and_symbol_tiebreak():
    watch = {"themes": [{"name": "T", "tickers": [{"symbol": s, "label": s} for s in ("D", "C", "B", "A")]}]}
    metrics = {s: metric("2026-10-03", z=z) for s, z in (("A", 2.5), ("B", -2.5), ("C", 2.5), ("D", 9.0))}
    r, _ = report.build_report(date(2026, 10, 6), watch, {}, {}, metrics, [], None)
    assert [m["symbol"] for m in r["movers"]] == ["D", "A", "B"]


def test_movers_weekly_uses_weekly_z():
    # 2026-10-05 は月曜
    r, _ = build(report_date=date(2026, 10, 5))
    assert r["edition"] == "weekly"
    assert [m["symbol"] for m in r["movers"]] == ["1000.T"]  # 週次 z 2.5 のみ
    assert r["movers"][0]["z"] == 2.5
    # tickers[].z は edition に関わらず日次 z
    assert next(t for t in r["tickers"] if t["symbol"] == "1000.T")["z"] == 3.1
    assert r["headline"].startswith("先週は")


def test_movers_exclude_macro():
    metrics = {"CL=F": metric("2026-10-03", z=9.0, wz=9.0)}
    r, _ = build(metrics)
    assert r["movers"] == []


def test_macro_follows_config_order():
    r, _ = build()
    assert [m["symbol"] for m in r["macro"]] == ["CL=F", "^N225"]
    assert r["macro"][0]["close"] == 91.11 and r["macro"][0]["daily_pct"] == -0.004
    assert r["macro"][1]["close"] is None


def test_fetch_failures_sorted_passthrough():
    r, _ = build(failures=["ZZZ", "AAA"])
    assert r["fetch_failures"] == ["AAA", "ZZZ"]


def test_state_returned_and_stale_wired_through():
    state = {"report_date": "2026-10-05", "asof": {"JP": "2026-10-05", "US": "2026-10-02"}, "prev_asof": {}}
    r, new_state = build(state=state)
    assert r["asof"]["JP_stale"] is True   # JP asof 10-05 は前回と同じ
    assert r["asof"]["US_stale"] is False  # US 10-03 は前回 10-02 より進んだ
    assert new_state["asof"] == {"JP": "2026-10-05", "US": "2026-10-03"}


def test_empty_metrics_does_not_crash():
    r, _ = build({})
    assert r["asof"]["JP"] is None and r["asof"]["US"] is None
    assert r["asof"]["JP_stale"] is False
    assert r["headline"] == "株価データなし"
    assert all(t["stale"] is False for t in r["tickers"])


# ---------------------------------------------------------------- markdown

def test_markdown_renders_main_sections():
    r, _ = build(state={"report_date": "2026-10-05", "asof": {"JP": "2026-10-05", "US": "2026-10-03"},
                        "prev_asof": {}})
    md = report.render_markdown(r, None)
    assert md.startswith("# stock-watch レポート 2026-10-06")
    assert r["headline"] in md
    assert "JP 2026-10-05（前回から更新なし）" in md
    for section in ("## テーマ別", "## 注目銘柄", "## マクロ指標", "## 固定ウォッチ", "## universe 検出", "## 取得失敗"):
        assert section in md
    assert "🏆" in md


def test_markdown_with_no_movers_and_failures():
    r, _ = build({}, failures=["ZZZ"])
    md = report.render_markdown(r, None)
    assert "- ZZZ" in md


def test_fmt_pct_never_shows_negative_zero():
    assert report.fmt_pct(-0.0004) == "+0.0%"
    assert report.fmt_pct(-0.0006) == "-0.1%"
    assert report.fmt_pct(0.052) == "+5.2%"
    assert report.fmt_pct(None) == "—"
