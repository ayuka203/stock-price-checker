"""DB を読み、変動条件に該当する銘柄をフラグする。

判定結果は report.py が読み込む data/flags.json に出力する
（DB テーブルは prices の 1 本のみとする方針のため、受け渡しはファイル経由）。
"""

import json
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "prices.db"
FLAGS_PATH = BASE_DIR / "data" / "flags.json"

# 前日比の変動率がこの値以上なら daily_move フラグ
DAILY_MOVE_THRESHOLD = 0.05
# 5営業日前比の変動率がこの値以上なら weekly_move フラグ
WEEKLY_MOVE_THRESHOLD = 0.10
# 出来高が直近 N 営業日平均の何倍以上なら volume_spike フラグ
VOLUME_SPIKE_MULTIPLIER = 3.0
# volume_spike の平均算出に使う直近営業日数
VOLUME_SPIKE_LOOKBACK = 20


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


def screen_symbol(history):
    """1銘柄分の (date, close, volume) 履歴（日付昇順）からフラグを判定する。

    比較対象日のデータが不足している場合、そのフラグは判定不能としてスキップする。
    """
    flags = {}
    _, latest_close, latest_volume = history[-1]

    if len(history) >= 2 and latest_close is not None:
        prev_close = history[-2][1]
        if prev_close:
            pct = (latest_close - prev_close) / prev_close
            if abs(pct) >= DAILY_MOVE_THRESHOLD:
                flags["daily_move"] = pct

    if len(history) >= 6 and latest_close is not None:
        base_close = history[-6][1]
        if base_close:
            pct = (latest_close - base_close) / base_close
            if abs(pct) >= WEEKLY_MOVE_THRESHOLD:
                flags["weekly_move"] = pct

    if len(history) >= VOLUME_SPIKE_LOOKBACK + 1 and latest_volume is not None:
        window = [v for (_, _, v) in history[-(VOLUME_SPIKE_LOOKBACK + 1):-1]]
        if window and all(v is not None for v in window):
            avg = sum(window) / len(window)
            if avg > 0 and latest_volume >= avg * VOLUME_SPIKE_MULTIPLIER:
                flags["volume_spike"] = latest_volume / avg

    if len(history) >= 2 and latest_close is not None:
        past_closes = [c for (_, c, _) in history[:-1] if c is not None]
        if past_closes:
            if latest_close > max(past_closes):
                flags["high_low"] = "high"
            elif latest_close < min(past_closes):
                flags["high_low"] = "low"

    return flags


def main():
    series = load_series()
    result = {}
    for symbol, history in series.items():
        if not history:
            continue
        flags = screen_symbol(history)
        if flags:
            result[symbol] = flags

    FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FLAGS_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"flagged {len(result)} symbols")


if __name__ == "__main__":
    main()
