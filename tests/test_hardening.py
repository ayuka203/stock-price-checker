"""stale 連動・非営業日の状態保護・非有限値・読み込み耐性のテスト。"""

import json
import math
from datetime import date

import pytest

import report
import screen
from test_report import build, metric
from test_screen import alternating_closes, make_history

STATE_FRI = {"report_date": "2026-10-02", "asof": {"JP": "2026-10-01", "US": "2026-10-01"}, "prev_asof": {}}


def mover_metrics(jp_date="2026-10-05", us_date="2026-10-05"):
    return {
        "1000.T": metric(jp_date, z=3.0, flags=["52w_high"]),
        "AAA": metric(us_date, z=-2.5),
        "BBB": metric(us_date, z=2.2),
    }


# ---------------------------------------------------------------- stale と movers

def test_market_stale_symbols_are_not_movers():
    # US が前回(10-05)から進んでいない → US 銘柄は movers から外れ、JP は残る
    state = {**STATE_FRI, "asof": {"JP": "2026-10-01", "US": "2026-10-05"}}
    r, _ = build(mover_metrics(), report_date=date(2026, 10, 6), state=state)
    assert r["asof"]["US_stale"] is True and r["asof"]["JP_stale"] is False
    assert [m["symbol"] for m in r["movers"]] == ["1000.T"]


def test_both_markets_stale_gives_no_movers():
    state = {**STATE_FRI, "asof": {"JP": "2026-10-05", "US": "2026-10-05"}}
    r, _ = build(mover_metrics(), report_date=date(2026, 10, 6), state=state)
    assert r["movers"] == []


def test_ticker_stale_symbol_is_not_a_mover():
    metrics = mover_metrics()
    metrics["BBB"] = metric("2026-10-02", z=2.2)  # US の他銘柄より古い
    r, _ = build(metrics, report_date=date(2026, 10, 6), state=STATE_FRI)
    assert [m["symbol"] for m in r["movers"]] == ["1000.T", "AAA"]


def test_non_stale_symbols_remain_movers():
    r, _ = build(mover_metrics(), report_date=date(2026, 10, 6), state=STATE_FRI)
    assert [m["symbol"] for m in r["movers"]] == ["1000.T", "AAA", "BBB"]


def test_market_stale_propagates_to_all_tickers_in_market():
    state = {**STATE_FRI, "asof": {"JP": "2026-10-01", "US": "2026-10-05"}}
    r, _ = build(mover_metrics(), report_date=date(2026, 10, 6), state=state)
    by = {t["symbol"]: t["stale"] for t in r["tickers"]}
    # 2000.T は JP だがデータ無し → 遅れ判定で stale
    assert by == {"1000.T": False, "AAA": True, "BBB": True, "2000.T": True}


def test_markdown_pause_mark_follows_market_stale():
    state = {**STATE_FRI, "asof": {"JP": "2026-10-01", "US": "2026-10-05"}}
    r, _ = build(mover_metrics(), report_date=date(2026, 10, 6), state=state)
    md = report.render_markdown(r, None)
    aaa_rows = [line for line in md.splitlines() if "| AAA |" in line and line.rstrip().endswith("⏸ |")]
    assert aaa_rows


# ---------------------------------------------------------------- 非営業日の状態

def test_weekend_run_does_not_return_state():
    sat, new_state = build(mover_metrics("2026-10-02", "2026-10-02"), report_date=date(2026, 10, 3), state=STATE_FRI)
    assert new_state is None
    assert sat["asof"]["JP_stale"] is False  # 金曜 07:30 の asof 10-01 から進んでいる
    _, new_state = build(mover_metrics("2026-10-02", "2026-10-02"), report_date=date(2026, 10, 4), state=STATE_FRI)
    assert new_state is None


def test_saturday_run_then_monday_run_is_not_stale():
    # 金曜の定期実行後の状態 → 土曜の手動実行（状態は書かれない）→ 月曜の定期実行
    state = STATE_FRI
    _, after_sat = build(mover_metrics("2026-10-02", "2026-10-02"), report_date=date(2026, 10, 3), state=state)
    if after_sat is not None:
        state = after_sat
    mon, new_state = build(mover_metrics("2026-10-02", "2026-10-02"), report_date=date(2026, 10, 5), state=state)
    assert mon["asof"]["JP_stale"] is False and mon["asof"]["US_stale"] is False
    assert all(t["stale"] is False for t in mon["tickers"] if t["asof"])
    assert new_state is not None and new_state["asof"]["JP"] == "2026-10-02"


def test_weekday_run_returns_state():
    _, new_state = build(mover_metrics(), report_date=date(2026, 10, 6), state=STATE_FRI)
    assert new_state is not None


# ---------------------------------------------------------------- 非有限値

def test_rnd_returns_none_for_non_finite():
    for bad in (float("inf"), float("-inf"), float("nan"), None):
        assert report.rnd(bad, 2) is None
    assert report.rnd(1.2345, 2) == 1.23


def test_infinite_metrics_never_reach_json():
    metrics = {
        "1000.T": metric("2026-10-05", close=float("inf"), daily=float("inf"), weekly=float("nan"),
                         z=float("inf"), wz=float("-inf")),
        "AAA": metric("2026-10-05", daily=0.03),
        "CL=F": metric("2026-10-05", close=float("nan"), daily=float("inf")),
    }
    r, _ = build(metrics)
    text = report.dumps_json(r)
    assert "Infinity" not in text and "NaN" not in text
    assert r["movers"] == []
    w1 = r["themes"][0]
    assert w1["daily_pct"] == pytest.approx(0.03) and w1["n"] == 1


def test_dumps_json_rejects_nan():
    with pytest.raises(ValueError):
        report.dumps_json({"x": math.nan})


def test_infinite_close_and_volume_are_dropped_in_screen():
    closes = alternating_closes(60)
    history = make_history(
        closes + [float("inf"), float("nan"), closes[-1] * 1.05],
        [1000] * 60 + [1000, 1000, float("inf")],
    )
    r = screen.screen_symbol(history)
    assert r["close"] == pytest.approx(closes[-1] * 1.05)
    assert r["date"] == "d0062"
    assert "volume_spike" not in r["flags"]  # 最新出来高が非有限 → 欠損扱い
    json.dumps(r, allow_nan=False)


def test_only_infinite_closes_returns_none():
    assert screen.screen_symbol(make_history([float("inf"), float("-inf")])) is None


def test_negative_close_is_kept_but_pct_needs_positive_base():
    r = screen.screen_symbol(make_history([10.0, 5.0, -3.0, 4.0]))
    assert r["close"] == 4.0
    assert r["daily_pct"] is None  # 基準が負
    r = screen.screen_symbol(make_history([10.0, 5.0, -3.0]))
    assert r["daily_pct"] == pytest.approx(-1.6)  # 基準が正なら計算する
    assert r["close"] == -3.0


def test_negative_closes_do_not_crash_judgement():
    closes = [-5.0 + 0.01 * i for i in range(260)]
    r = screen.screen_symbol(make_history(closes))
    assert r["close"] == pytest.approx(closes[-1])
    json.dumps(r, allow_nan=False)


def test_screen_all_output_is_strict_json_with_inf_in_history():
    closes = alternating_closes(60)
    series = {
        "AAA": make_history(closes + [float("inf")]),
        "CL=F": make_history([float("nan"), float("inf"), 90.0, 91.0]),
    }
    out = screen.screen_all(series, {"CL=F"})
    text = json.dumps(out, allow_nan=False)
    assert "Infinity" not in text and "NaN" not in text


# ---------------------------------------------------------------- 読み込み耐性・原子的書き込み

def test_load_json_recovers_from_corrupt_file(tmp_path, capsys):
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    assert report.load_json(p, None, dict) is None
    assert "warning" in capsys.readouterr().err


def test_load_json_recovers_from_invalid_utf8(tmp_path):
    p = tmp_path / "state.json"
    p.write_bytes(b"\xff\xfe\x00")
    assert report.load_json(p, {}, dict) == {}


def test_load_json_recovers_from_wrong_type(tmp_path, capsys):
    p = tmp_path / "flags.json"
    p.write_text("[1, 2]", encoding="utf-8")
    assert report.load_json(p, {}, dict) == {}
    assert "should be dict" in capsys.readouterr().err
    p.write_text("{}", encoding="utf-8")
    assert report.load_json(p, [], list) == []


def test_load_json_missing_and_valid(tmp_path):
    assert report.load_json(tmp_path / "none.json", "d", dict) == "d"
    p = tmp_path / "ok.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert report.load_json(p, {}, dict) == {"a": 1}


def test_corrupt_state_means_first_run_semantics(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("[]", encoding="utf-8")
    state = report.load_json(p, None, dict)
    r, _ = build(state=state)
    assert r["asof"]["JP_stale"] is False


def test_write_text_atomic_replaces_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "asof_state.json"
    p.write_text("old", encoding="utf-8")
    report.write_text_atomic(p, "new")
    assert p.read_text(encoding="utf-8") == "new"
    assert [f.name for f in tmp_path.iterdir()] == ["asof_state.json"]
