"""株価取得と SQLite への保存。

watchlist.yaml / universe.yaml / macro.yaml の symbol を統合し、yfinance で
一括取得して data/prices.db の prices テーブルに upsert する。

毎回 2 年分を取得して INSERT OR REPLACE で上書きするため、株式分割などの
調整が過去行にも遡って反映される。
"""

import json
import math
import sqlite3
import sys
import time
from pathlib import Path

import yaml
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "prices.db"
FAILURES_PATH = DATA_DIR / "fetch_failures.json"

# 250 営業日の窓に対し、日本株は 1y で約 243 行しか取れず分割調整が窓の端に届かないため 2y
FETCH_PERIOD = "2y"
# 個別再試行の前に置く間隔（秒）。tz キャッシュ sqlite のロック解消待ち
RETRY_SLEEP_SEC = 1.0


def _load_cfg(path):
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_symbols(path):
    cfg = _load_cfg(path)
    symbols = []
    for theme in cfg.get("themes", []):
        for ticker in theme.get("tickers", []):
            symbols.append(ticker["symbol"])
    return symbols


def load_macro_symbols(path):
    cfg = _load_cfg(path)
    return [item["symbol"] for item in cfg.get("macro", [])]


def collect_all_symbols():
    candidates = (
        load_symbols(CONFIG_DIR / "watchlist.yaml")
        + load_symbols(CONFIG_DIR / "universe.yaml")
        + load_macro_symbols(CONFIG_DIR / "macro.yaml")
    )
    symbols = []
    for symbol in candidates:
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def ensure_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
          date   TEXT NOT NULL,
          symbol TEXT NOT NULL,
          close  REAL,
          volume INTEGER,
          PRIMARY KEY (date, symbol)
        )
        """
    )
    conn.commit()
    return conn


def fetch_prices(symbols, threads=True):
    return yf.download(
        symbols,
        period=FETCH_PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=threads,
    )


def extract_symbol_frame(data, symbol):
    """yf.download の戻り値から銘柄ごとの DataFrame を取り出す。

    group_by="ticker" では (ticker, field) の MultiIndex 列になる。
    単一銘柄でフラットな列で返るバージョンにも備える。
    """
    columns = getattr(data, "columns", None)
    if columns is None:
        return None
    if getattr(columns, "nlevels", 1) > 1:
        try:
            return data[symbol]
        except (KeyError, IndexError):
            return None
    return data


def is_usable(frame):
    return (
        frame is not None
        and not frame.empty
        and "Close" in frame
        and not frame["Close"].dropna().empty
    )


def upsert_rows(conn, symbol, frame):
    """終値が有限値の行だけを保存する。負の終値は有効（CL=F など）。出来高が非有限なら NULL。"""
    count = 0
    for idx, row in frame.iterrows():
        try:
            close = float(row.get("Close"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close):  # NaN = その日のデータなし、inf = 異常値
            continue
        vol_val = None
        try:
            volume = float(row.get("Volume"))
            if math.isfinite(volume):
                vol_val = int(volume)
        except (TypeError, ValueError, OverflowError):
            pass
        date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        conn.execute(
            "INSERT OR REPLACE INTO prices (date, symbol, close, volume) VALUES (?, ?, ?, ?)",
            (date_str, symbol, close, vol_val),
        )
        count += 1
    return count


def fetch_frames(symbols, downloader=fetch_prices):
    """一括取得し、失敗した銘柄は threads=False で個別に 1 回だけ再試行する。

    戻り値は ({symbol: frame}, [失敗した symbol])。
    yfinance の tz キャッシュ sqlite が一時的に "database is locked" になるため、
    並列取得での失敗は直列で取り直す。
    """
    frames = {}
    try:
        raw = downloader(symbols)
    except Exception as exc:  # ネットワーク断など。全銘柄を個別再試行に回す
        print(f"bulk fetch failed: {exc}", file=sys.stderr)
        raw = None

    retry = []
    for symbol in symbols:
        frame = extract_symbol_frame(raw, symbol) if raw is not None else None
        if is_usable(frame):
            frames[symbol] = frame
        else:
            retry.append(symbol)

    failures = []
    for symbol in retry:
        time.sleep(RETRY_SLEEP_SEC)
        try:
            frame = extract_symbol_frame(downloader([symbol], threads=False), symbol)
        except Exception as exc:
            print(f"retry failed for {symbol}: {exc}", file=sys.stderr)
            frame = None
        if is_usable(frame):
            frames[symbol] = frame
        else:
            failures.append(symbol)
    return frames, failures


def main():
    symbols = collect_all_symbols()
    if not symbols:
        print("no symbols configured in watchlist.yaml / universe.yaml / macro.yaml", file=sys.stderr)
        sys.exit(1)

    conn = ensure_db()
    frames, failures = fetch_frames(symbols)

    for symbol, frame in frames.items():
        upsert_rows(conn, symbol, frame)

    conn.commit()
    conn.close()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAILURES_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(failures), f, ensure_ascii=False, indent=2)

    if not frames:
        print("all symbols failed to fetch", file=sys.stderr)
        sys.exit(1)

    print(f"fetched {len(frames)}/{len(symbols)} symbols ({len(failures)} failed)")


if __name__ == "__main__":
    main()
