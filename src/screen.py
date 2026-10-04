"""DB を読み、銘柄ごとの指標（騰落率・z値）とフラグを算出する。

結果は report.py が読み込む data/flags.json に出力する
（DB テーブルは prices の 1 本のみとする方針のため、受け渡しはファイル経由）。

判定はボラティリティ基準（z値）と 52 週高値・安値で行う。固定の騰落率閾値や
「DB 全期間」基準の高値・安値は使わない。マクロ指標（macro.yaml）は騰落率のみ
算出し、z値・フラグの判定対象外とする。
"""

import json
import math
import sqlite3
import statistics
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DB_PATH = BASE_DIR / "data" / "prices.db"
FLAGS_PATH = BASE_DIR / "data" / "flags.json"

# z値の分母（日次リターン標準偏差）に使う、直前の営業日数
Z_LOOKBACK = 60
# 標準偏差を算出するのに最低限必要なリターン個数
Z_MIN_RETURNS = 20
# 週次リターンの営業日数
WEEK_DAYS = 5
# 52週高値・安値の基準にする直前の営業日数と、判定に必要な最低履歴
HIGH_LOW_LOOKBACK = 250
HIGH_LOW_MIN_HISTORY = 200
# 出来高急増: 直前 N 営業日平均の何倍以上か、かつ同日の |z| がいくつ以上か
VOLUME_SPIKE_LOOKBACK = 20
VOLUME_SPIKE_MULTIPLIER = 3.0
VOLUME_SPIKE_MIN_ABS_Z = 1.5


def load_series():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date, symbol, close, volume FROM prices ORDER BY symbol, date"
    ).fetchall()
    conn.close()
    series = {}
    for date, symbol, close, volume in rows:
        series.setdefault(symbol, []).append((date, close, volume))
    return series


def load_macro_symbols():
    path = CONFIG_DIR / "macro.yaml"
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return {item["symbol"] for item in cfg.get("macro", [])}


def _is_finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _finite_or_none(value):
    return value if _is_finite(value) else None


def pct_change(closes, n):
    """n 営業日前の終値に対する最新終値の騰落率。基準が無い・0 以下なら None。"""
    if len(closes) < n + 1:
        return None
    base = closes[-(n + 1)]
    if not base or base <= 0:
        return None
    return closes[-1] / base - 1


def daily_sigma(closes):
    """最新リターンを除く直前 Z_LOOKBACK 個の日次リターンの標準偏差（標本）。

    リターンが Z_MIN_RETURNS 個未満、または標準偏差が 0 なら None。
    """
    returns = [
        closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0
    ]
    window = returns[-(Z_LOOKBACK + 1):-1]
    if len(window) < Z_MIN_RETURNS:
        return None
    sigma = statistics.stdev(window)
    return sigma if sigma > 0 else None


def new_extreme(closes):
    """最新終値が 52 週高値・安値を「初めて」抜けたなら 52w_high / 52w_low を返す。

    基準は最新日を除く直前 HIGH_LOW_LOOKBACK 営業日の終値。直前の履歴が
    HIGH_LOW_MIN_HISTORY 営業日未満なら判定しない。前日終値が前日時点の基準で
    すでに同方向へ更新していた場合は点灯しない。
    """
    prior = closes[-(HIGH_LOW_LOOKBACK + 1):-1]
    if len(prior) < HIGH_LOW_MIN_HISTORY:
        return None
    latest = closes[-1]
    if latest > max(prior):
        kind, broke_before = "52w_high", _broke(closes, "high")
    elif latest < min(prior):
        kind, broke_before = "52w_low", _broke(closes, "low")
    else:
        return None
    return None if broke_before else kind


def _broke(closes, direction):
    """前日終値が、前日時点の直前 HIGH_LOW_LOOKBACK 営業日基準で更新済みだったか。"""
    prev = closes[-2]
    prev_prior = closes[-(HIGH_LOW_LOOKBACK + 2):-2]
    if len(prev_prior) < HIGH_LOW_MIN_HISTORY:
        return False
    return prev > max(prev_prior) if direction == "high" else prev < min(prev_prior)


def is_volume_spike(volumes, z):
    """volumes は最新日が末尾。直前平均の 3 倍以上、かつ |z| >= 1.5 のとき True。"""
    if z is None or abs(z) < VOLUME_SPIKE_MIN_ABS_Z:
        return False
    if len(volumes) < VOLUME_SPIKE_LOOKBACK + 1:
        return False
    latest = volumes[-1]
    window = volumes[-(VOLUME_SPIKE_LOOKBACK + 1):-1]
    if latest is None or any(v is None for v in window):
        return False
    avg = sum(window) / len(window)
    return avg > 0 and latest >= avg * VOLUME_SPIKE_MULTIPLIER


def screen_symbol(history, judge=True):
    """1銘柄分の (date, close, volume) 履歴（日付昇順）から指標とフラグを算出する。

    終値が欠損・非有限の行は除外する（負の終値は有効。騰落率は基準値が正のときだけ
    計算する）。履歴が空なら None。judge=False では騰落率のみ算出する。
    出来高が非有限なら欠損扱い。戻り値の数値はすべて有限値か None。
    """
    rows = [(d, c, _finite_or_none(v)) for (d, c, v) in history if _is_finite(c)]
    if not rows:
        return None
    closes = [c for (_, c, _) in rows]
    volumes = [v for (_, _, v) in rows]

    result = {
        "date": rows[-1][0],
        "close": closes[-1],
        "daily_pct": _finite_or_none(pct_change(closes, 1)),
        "weekly_pct": _finite_or_none(pct_change(closes, WEEK_DAYS)),
        "z": None,
        "weekly_z": None,
        "flags": [],
    }
    if not judge:
        return result

    sigma = daily_sigma(closes)
    if sigma is not None:
        if result["daily_pct"] is not None:
            result["z"] = _finite_or_none(result["daily_pct"] / sigma)
        if result["weekly_pct"] is not None:
            result["weekly_z"] = _finite_or_none(result["weekly_pct"] / (sigma * math.sqrt(WEEK_DAYS)))

    extreme = new_extreme(closes)
    if extreme:
        result["flags"].append(extreme)
    if is_volume_spike(volumes, result["z"]):
        result["flags"].append("volume_spike")
    return result


def screen_all(series, macro_symbols):
    result = {}
    for symbol, history in series.items():
        screened = screen_symbol(history, judge=symbol not in macro_symbols)
        if screened is not None:
            result[symbol] = screened
    return result


def main():
    result = screen_all(load_series(), load_macro_symbols())

    FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FLAGS_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)

    flagged = sum(1 for v in result.values() if v["flags"])
    print(f"screened {len(result)} symbols ({flagged} flagged)")


if __name__ == "__main__":
    main()
