import sqlite3
from pathlib import Path

import pandas as pd
import pytest
import yaml

import fetch

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fetch.time, "sleep", sleeps.append)
    return sleeps


def frame(closes, start="2026-09-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes, "Volume": [100] * len(closes)}, index=idx)


def multi(frames):
    """yf.download(group_by="ticker") 相当の MultiIndex 列 DataFrame。"""
    return pd.concat(frames, axis=1)


class FakeDownloader:
    def __init__(self, bulk, single=None, raise_bulk=False):
        self.bulk = bulk
        self.single = single or {}
        self.raise_bulk = raise_bulk
        self.calls = []

    def __call__(self, symbols, threads=True):
        self.calls.append((list(symbols), threads))
        if len(symbols) > 1 or threads:
            if self.raise_bulk:
                raise RuntimeError("database is locked")
            return self.bulk
        result = self.single.get(symbols[0])
        if isinstance(result, Exception):
            raise result
        return result if result is not None else pd.DataFrame()


# ---------------------------------------------------------------- fetch_frames

def test_failed_symbol_is_retried_once_with_threads_false():
    bulk = multi({"AAA": frame([1.0, 2.0]), "BBB": frame([float("nan"), float("nan")])})
    dl = FakeDownloader(bulk, single={"BBB": multi({"BBB": frame([5.0, 6.0])})})
    frames, failures = fetch.fetch_frames(["AAA", "BBB"], downloader=dl)
    assert set(frames) == {"AAA", "BBB"} and failures == []
    assert dl.calls == [(["AAA", "BBB"], True), (["BBB"], False)]


def test_symbol_failing_again_is_reported_and_not_retried_twice():
    bulk = multi({"AAA": frame([1.0, 2.0]), "BBB": frame([float("nan")] * 2)})
    dl = FakeDownloader(bulk, single={})
    frames, failures = fetch.fetch_frames(["AAA", "BBB"], downloader=dl)
    assert list(frames) == ["AAA"] and failures == ["BBB"]
    assert dl.calls.count((["BBB"], False)) == 1


def test_retry_exception_is_a_failure_not_a_crash():
    bulk = multi({"AAA": frame([1.0, 2.0])})
    dl = FakeDownloader(bulk, single={"BBB": RuntimeError("database is locked")})
    frames, failures = fetch.fetch_frames(["AAA", "BBB"], downloader=dl)
    assert list(frames) == ["AAA"] and failures == ["BBB"]


def test_bulk_exception_falls_back_to_individual_retries():
    dl = FakeDownloader(None, single={"AAA": multi({"AAA": frame([1.0, 2.0])})}, raise_bulk=True)
    frames, failures = fetch.fetch_frames(["AAA", "BBB"], downloader=dl)
    assert list(frames) == ["AAA"] and failures == ["BBB"]


def test_flat_columns_single_symbol_frame():
    f = frame([1.0, 2.0])
    assert fetch.extract_symbol_frame(f, "AAA") is f
    assert fetch.extract_symbol_frame(multi({"AAA": f}), "AAA") is not None
    assert fetch.extract_symbol_frame(multi({"AAA": f}), "ZZZ") is None


# ---------------------------------------------------------------- upsert

def test_upsert_overwrites_old_rows_without_schema_change():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE prices (date TEXT NOT NULL, symbol TEXT NOT NULL, close REAL, volume INTEGER,"
        " PRIMARY KEY (date, symbol))"
    )
    fetch.upsert_rows(conn, "AAA", frame([100.0, 110.0]))
    fetch.upsert_rows(conn, "AAA", frame([50.0, 55.0]))  # 分割調整後の値で再取得
    rows = conn.execute("SELECT close FROM prices WHERE symbol='AAA' ORDER BY date").fetchall()
    assert rows == [(50.0,), (55.0,)]


def test_upsert_skips_nan_close():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE prices (date TEXT NOT NULL, symbol TEXT NOT NULL, close REAL, volume INTEGER,"
        " PRIMARY KEY (date, symbol))"
    )
    assert fetch.upsert_rows(conn, "AAA", frame([100.0, float("nan")])) == 1


def test_fetch_period_is_two_years():
    assert fetch.FETCH_PERIOD == "2y"


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE prices (date TEXT NOT NULL, symbol TEXT NOT NULL, close REAL, volume INTEGER,"
        " PRIMARY KEY (date, symbol))"
    )
    return conn


def test_upsert_drops_infinite_close_and_nulls_bad_volume():
    conn = make_conn()
    f = frame([100.0, float("inf"), 102.0, float("-inf")])
    f["Volume"] = [100, 100, float("inf"), float("nan")]
    assert fetch.upsert_rows(conn, "AAA", f) == 2
    rows = conn.execute("SELECT close, volume FROM prices ORDER BY date").fetchall()
    assert rows == [(100.0, 100), (102.0, None)]


def test_upsert_keeps_negative_close():
    conn = make_conn()
    assert fetch.upsert_rows(conn, "CL=F", frame([-37.63, 5.0])) == 2
    assert conn.execute("SELECT close FROM prices ORDER BY date").fetchall() == [(-37.63,), (5.0,)]


def test_upsert_survives_unconvertible_values():
    conn = make_conn()
    f = frame([100.0, 101.0])
    f["Volume"] = [float("inf"), None]
    assert fetch.upsert_rows(conn, "AAA", f) == 2


def test_sleep_before_each_individual_retry(no_sleep):
    bulk = multi({"AAA": frame([1.0, 2.0]), "BBB": frame([float("nan")] * 2), "CCC": frame([float("nan")] * 2)})
    fetch.fetch_frames(["AAA", "BBB", "CCC"], downloader=FakeDownloader(bulk))
    assert no_sleep == [fetch.RETRY_SLEEP_SEC, fetch.RETRY_SLEEP_SEC]
    assert fetch.RETRY_SLEEP_SEC == 1.0


def test_no_sleep_when_nothing_to_retry(no_sleep):
    fetch.fetch_frames(["AAA"], downloader=FakeDownloader(multi({"AAA": frame([1.0, 2.0])})))
    assert no_sleep == []


# ---------------------------------------------------------------- 設定ファイル

def load(name):
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def all_tickers():
    for name in ("watchlist.yaml", "universe.yaml"):
        for theme in load(name)["themes"]:
            for ticker in theme["tickers"]:
                yield theme["name"], ticker


def test_macro_yaml_schema_and_content():
    macro = load("macro.yaml")["macro"]
    assert [m["symbol"] for m in macro] == ["CL=F", "NG=F", "JPY=X", "^N225", "^GSPC", "SRUUF"]
    assert all(set(m) == {"symbol", "label"} for m in macro)


def test_every_ticker_has_keywords():
    for _, ticker in all_tickers():
        assert isinstance(ticker.get("keywords"), list) and ticker["keywords"], ticker["symbol"]
        assert all(isinstance(k, str) and k for k in ticker["keywords"])


def test_fujikura_added_to_transmission_theme():
    themes = {t["name"]: t for t in load("universe.yaml")["themes"]}
    fujikura = [t for t in themes["送配電・計測"]["tickers"] if t["symbol"] == "5803.T"]
    assert fujikura == [{"symbol": "5803.T", "label": "フジクラ", "keywords": ["フジクラ"]}]


def test_requirements_are_range_pinned():
    root = CONFIG_DIR.parent
    assert (root / "requirements.txt").read_text().split() == ["yfinance>=1.7,<2", "pyyaml>=6,<7"]
    assert (root / "requirements-dev.txt").read_text().split() == ["pytest>=9.0.3,<10"]


def test_claude_dir_is_gitignored():
    assert ".claude/" in (CONFIG_DIR.parent / ".gitignore").read_text().split()


def test_collect_all_symbols_includes_macro_without_duplicates():
    symbols = fetch.collect_all_symbols()
    assert len(symbols) == len(set(symbols))
    for s in ("CL=F", "NG=F", "JPY=X", "^N225", "^GSPC", "SRUUF", "5803.T", "7011.T"):
        assert s in symbols
