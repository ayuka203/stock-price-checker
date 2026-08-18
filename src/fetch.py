"""株価取得と SQLite への保存。

watchlist.yaml / universe.yaml の symbol を統合し、yfinance で一括取得して
data/prices.db の prices テーブルに upsert する。
"""

import json
import sqlite3
import sys
from pathlib import Path

import yaml
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "prices.db"
FAILURES_PATH = DATA_DIR / "fetch_failures.json"


def load_symbols(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    symbols = []
    for theme in cfg.get("themes", []):
        for ticker in theme.get("tickers", []):
            symbols.append(ticker["symbol"])
    return symbols


def collect_all_symbols():
    watchlist_symbols = load_symbols(CONFIG_DIR / "watchlist.yaml")
    universe_symbols = load_symbols(CONFIG_DIR / "universe.yaml")
    symbols = []
    for symbol in watchlist_symbols + universe_symbols:
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


def fetch_prices(symbols):
    return yf.download(
        symbols,
        period="5d",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
    )


def extract_symbol_frame(data, symbol, n_symbols):
    """yf.download の戻り値から銘柄ごとの DataFrame を取り出す。

    n_symbols == 1 のときは group_by="ticker" でも列がフラットに返る。
    """
    if n_symbols == 1:
        return data
    try:
        return data[symbol]
    except (KeyError, IndexError):
        return None


def upsert_rows(conn, symbol, frame):
    count = 0
    for idx, row in frame.iterrows():
        close = row.get("Close")
        if close is None or close != close:  # NaN close = その日のデータなし
            continue
        volume = row.get("Volume")
        vol_val = None if volume is None or volume != volume else int(volume)
        date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        conn.execute(
            "INSERT OR REPLACE INTO prices (date, symbol, close, volume) VALUES (?, ?, ?, ?)",
            (date_str, symbol, float(close), vol_val),
        )
        count += 1
    return count


def main():
    symbols = collect_all_symbols()
    if not symbols:
        print("no symbols configured in watchlist.yaml / universe.yaml", file=sys.stderr)
        sys.exit(1)

    conn = ensure_db()

    try:
        raw = fetch_prices(symbols)
    except Exception as exc:  # ネットワーク断など、全銘柄取得不能につながる異常
        print(f"fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    failures = []
    succeeded = 0
    for symbol in symbols:
        frame = extract_symbol_frame(raw, symbol, len(symbols))
        if frame is None or frame.empty or "Close" not in frame or frame["Close"].dropna().empty:
            failures.append(symbol)
            continue
        upsert_rows(conn, symbol, frame)
        succeeded += 1

    conn.commit()
    conn.close()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(FAILURES_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(failures), f, ensure_ascii=False, indent=2)

    if succeeded == 0:
        print("all symbols failed to fetch", file=sys.stderr)
        sys.exit(1)

    print(f"fetched {succeeded}/{len(symbols)} symbols ({len(failures)} failed)")


if __name__ == "__main__":
    main()
