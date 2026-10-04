import math
import statistics

import pytest

import screen


def make_history(closes, volumes=None):
    """合成の (date, close, volume) 履歴を作る。日付は連番の ISO 文字列。"""
    if volumes is None:
        volumes = [1000] * len(closes)
    return [(f"d{i:04d}", c, v) for i, (c, v) in enumerate(zip(closes, volumes))]


def alternating_closes(n, step=0.01, start=100.0):
    """+step / -step を交互に繰り返す終値列（長さ n）。"""
    closes = [start]
    for i in range(1, n):
        closes.append(closes[-1] * (1 + step if i % 2 else 1 - step))
    return closes


def flat_then(n_flat, tail, start=100.0):
    return [start] * n_flat + list(tail)


# ---------------------------------------------------------------- 境界値

def test_empty_history_returns_none():
    assert screen.screen_symbol([]) is None


def test_all_missing_closes_returns_none():
    assert screen.screen_symbol(make_history([None, None])) is None


def test_single_row():
    r = screen.screen_symbol(make_history([100.0]))
    assert r["close"] == 100.0
    assert r["daily_pct"] is None and r["weekly_pct"] is None
    assert r["z"] is None and r["weekly_z"] is None
    assert r["flags"] == []


def test_two_rows_has_daily_but_no_z():
    r = screen.screen_symbol(make_history([100.0, 110.0]))
    assert r["daily_pct"] == pytest.approx(0.10)
    assert r["weekly_pct"] is None
    assert r["z"] is None


def test_rows_with_missing_close_are_dropped():
    r = screen.screen_symbol(make_history([100.0, None, 110.0]))
    assert r["daily_pct"] == pytest.approx(0.10)
    assert r["date"] == "d0002"


def test_weekly_pct_needs_six_rows():
    assert screen.screen_symbol(make_history([100.0] * 5))["weekly_pct"] is None
    r = screen.screen_symbol(make_history([100.0] * 5 + [110.0]))
    assert r["weekly_pct"] == pytest.approx(0.10)


# ---------------------------------------------------------------- z値

def test_z_none_below_20_returns():
    # 直前リターンが 19 個 + 最新 1 個 = 21 終値 + 1 → 22 終値で 20 個
    closes = alternating_closes(21)
    assert screen.screen_symbol(make_history(closes))["z"] is None


def test_z_defined_with_exactly_20_prior_returns():
    closes = alternating_closes(22)
    assert screen.screen_symbol(make_history(closes))["z"] is not None


def test_z_value_matches_manual_calculation():
    closes = alternating_closes(61)  # 60 リターン
    closes.append(closes[-1] * 1.05)
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    expected_sigma = statistics.stdev(returns[-61:-1])
    r = screen.screen_symbol(make_history(closes))
    assert r["z"] == pytest.approx(0.05 / expected_sigma)
    weekly = closes[-1] / closes[-6] - 1
    assert r["weekly_z"] == pytest.approx(weekly / (expected_sigma * math.sqrt(5)))


def test_z_uses_only_latest_60_prior_returns():
    # 古い大きな変動(61 個より前)は σ に入らない
    wild = [100.0, 150.0, 100.0, 150.0]
    calm = alternating_closes(62, step=0.01, start=100.0)
    closes = wild + calm
    closes.append(closes[-1] * 1.05)
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    expected = statistics.stdev(returns[-61:-1])
    r = screen.screen_symbol(make_history(closes))
    assert r["z"] == pytest.approx(0.05 / expected)


def test_z_none_when_sigma_zero():
    closes = flat_then(30, [105.0])
    r = screen.screen_symbol(make_history(closes))
    assert r["z"] is None and r["weekly_z"] is None


def test_negative_z():
    closes = alternating_closes(40)
    closes.append(closes[-1] * 0.9)
    assert screen.screen_symbol(make_history(closes))["z"] < -2


# ---------------------------------------------------------------- 52週高値・安値

def rising(n, start=100.0, step=0.1):
    return [start + step * i for i in range(n)]


def test_52w_not_judged_below_200_prior():
    closes = rising(199) + [1000.0]  # 直前 199 件
    assert screen.new_extreme(closes) is None
    assert screen.screen_symbol(make_history(closes))["flags"] == []


def test_52w_judged_at_exactly_200_prior():
    closes = flat_then(200, [200.0])  # 直前 200 件すべて 100
    assert screen.new_extreme(closes) == "52w_high"


def test_52w_low_at_exactly_200_prior():
    closes = flat_then(200, [50.0])
    assert screen.new_extreme(closes) == "52w_low"


def test_52w_no_flag_when_inside_range():
    closes = alternating_closes(260, step=0.01)
    assert screen.new_extreme(closes[:-1] + [closes[0]]) is None


def test_52w_high_only_first_day():
    base = alternating_closes(250, step=0.001)
    top = max(base)
    day1 = top * 1.02
    day2 = day1 * 1.02
    assert screen.new_extreme(base + [day1]) == "52w_high"
    assert screen.new_extreme(base + [day1, day2]) is None
    # 日をまたいで更新が続いても点灯しない
    assert screen.new_extreme(base + [day1, day2, day2 * 1.02]) is None


def test_52w_high_relights_after_pullback():
    base = alternating_closes(250, step=0.001)
    top = max(base)
    seq = base + [top * 1.02, top * 0.99, top * 1.05]
    assert screen.new_extreme(seq) == "52w_high"


def test_52w_low_only_first_day():
    base = alternating_closes(250, step=0.001)
    bottom = min(base)
    day1 = bottom * 0.98
    day2 = day1 * 0.98
    assert screen.new_extreme(base + [day1]) == "52w_low"
    assert screen.new_extreme(base + [day1, day2]) is None


def test_52w_prev_day_baseline_too_short_still_lights():
    # 直前履歴がちょうど 200 件のとき、前日は判定不能だったので今日が初回
    closes = flat_then(199, [150.0, 160.0])  # 前日 150 は 199 件基準で判定されない
    assert screen.new_extreme(closes) == "52w_high"


def test_52w_lookback_is_250_days():
    # 251 営業日前の高値は基準に入らない
    closes = [1000.0] + [100.0] * 250 + [110.0]
    assert screen.new_extreme(closes) == "52w_high"
    closes = [1000.0] + [100.0] * 249 + [110.0]  # 1000 が窓内
    assert screen.new_extreme(closes) is None


def test_52w_flag_in_screen_symbol():
    closes = flat_then(250, [120.0])
    assert screen.screen_symbol(make_history(closes))["flags"] == ["52w_high"]


# ---------------------------------------------------------------- 出来高急増

def spike_inputs(volume, z=2.0, n=21, base=1000):
    return [base] * (n - 1) + [volume], z


def test_volume_spike_true():
    vols, z = spike_inputs(3000)
    assert screen.is_volume_spike(vols, z)


def test_volume_spike_false_below_multiplier():
    vols, z = spike_inputs(2999)
    assert not screen.is_volume_spike(vols, z)


def test_volume_spike_false_when_z_small():
    vols, _ = spike_inputs(5000)
    assert not screen.is_volume_spike(vols, 1.49)
    assert screen.is_volume_spike(vols, 1.5)
    assert screen.is_volume_spike(vols, -1.5)


def test_volume_spike_false_when_z_none():
    vols, _ = spike_inputs(5000)
    assert not screen.is_volume_spike(vols, None)


def test_volume_spike_needs_20_prior_days():
    vols, z = spike_inputs(5000, n=20)
    assert not screen.is_volume_spike(vols, z)


def test_volume_spike_false_with_missing_volume():
    vols, z = spike_inputs(5000)
    vols[3] = None
    assert not screen.is_volume_spike(vols, z)
    vols, z = spike_inputs(5000)
    vols[-1] = None
    assert not screen.is_volume_spike(vols, z)


def test_volume_spike_false_zero_average():
    vols, z = spike_inputs(5000, base=0)
    assert not screen.is_volume_spike(vols, z)


def test_volume_spike_end_to_end():
    closes = alternating_closes(60)
    closes.append(closes[-1] * 1.06)
    volumes = [1000] * 60 + [4000]
    r = screen.screen_symbol(make_history(closes, volumes))
    assert "volume_spike" in r["flags"]
    # 出来高が同じでも値動きが小さければ点灯しない
    closes2 = alternating_closes(60)
    closes2.append(closes2[-1] * 1.0001)
    r2 = screen.screen_symbol(make_history(closes2, volumes))
    assert "volume_spike" not in r2["flags"]


# ---------------------------------------------------------------- マクロ除外

def test_judge_false_skips_z_and_flags():
    closes = flat_then(250, [120.0])
    r = screen.screen_symbol(make_history(closes), judge=False)
    assert r["z"] is None and r["weekly_z"] is None and r["flags"] == []
    assert r["close"] == 120.0


def test_screen_all_excludes_macro_from_judgement():
    closes = flat_then(250, [120.0])
    series = {"AAA": make_history(closes), "CL=F": make_history(closes), "EMPTY": []}
    out = screen.screen_all(series, {"CL=F"})
    assert out["AAA"]["flags"] == ["52w_high"]
    assert out["CL=F"]["flags"] == []
    assert out["CL=F"]["daily_pct"] == pytest.approx(0.2)
    assert "EMPTY" not in out
