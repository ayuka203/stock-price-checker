"""reports/YYYY-MM-DD.md を生成する。"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = DATA_DIR / "prices.db"
FLAGS_PATH = DATA_DIR / "flags.json"
FAILURES_PATH = DATA_DIR / "fetch_failures.json"
EVENTS_PATH = CONFIG_DIR / "events.yaml"

JST = ZoneInfo("Asia/Tokyo")

# events.yaml のうち実行日からこの日数以内のものだけをレポートに列挙する
EVENT_LOOKAHEAD_DAYS = 14

FLAG_EMOJI = {
    "daily_move": "🔴",
    "weekly_move": "🟠",
    "volume_spike": "🔵",
}
HIGH_LOW_EMOJI = {"high": "🏆", "low": "🧊"}


def load_yaml(path):
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_history():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date, symbol, close, volume FROM prices ORDER BY symbol, date"
    ).fetchall()
    conn.close()
    series = {}
    for date, symbol, close, volume in rows:
        series.setdefault(symbol, []).append((date, close, volume))
    return series


def compute_metrics(history):
    if not history:
        return None
    _, latest_close, _ = history[-1]
    daily_pct = None
    if len(history) >= 2 and history[-2][1]:
        daily_pct = (latest_close - history[-2][1]) / history[-2][1]
    weekly_pct = None
    if len(history) >= 6 and history[-6][1]:
        weekly_pct = (latest_close - history[-6][1]) / history[-6][1]
    return {"close": latest_close, "daily_pct": daily_pct, "weekly_pct": weekly_pct}


def fmt_price(value):
    return f"{value:.1f}" if value is not None else "—"


def fmt_pct(value):
    return f"{value * 100:+.1f}%" if value is not None else "—"


def flag_marks(symbol, flags_by_symbol):
    flags = flags_by_symbol.get(symbol, {})
    marks = [FLAG_EMOJI[key] for key in ("daily_move", "weekly_move", "volume_spike") if key in flags]
    if "high_low" in flags:
        marks.append(HIGH_LOW_EMOJI.get(flags["high_low"], ""))
    return "".join(marks)


def render_row(label, symbol, history, flags_by_symbol):
    metrics = compute_metrics(history)
    marks = flag_marks(symbol, flags_by_symbol)
    if metrics is None:
        return f"| {label} | {symbol} | — | — | — | {marks} |"
    return (
        f"| {label} | {symbol} | {fmt_price(metrics['close'])} | "
        f"{fmt_pct(metrics['daily_pct'])} | {fmt_pct(metrics['weekly_pct'])} | {marks} |"
    )


def build_watchlist_section(history, flags_by_symbol):
    cfg = load_yaml(CONFIG_DIR / "watchlist.yaml")
    lines = ["## 固定ウォッチ", ""]
    watchlist_symbols = set()
    for theme in cfg.get("themes", []):
        lines.append(f"### {theme.get('name', '')}")
        lines.append("")
        lines.append("| label | symbol | 終値 | 前日比 | 週間騰落 | flags |")
        lines.append("|---|---|---|---|---|---|")
        for ticker in theme.get("tickers", []):
            symbol = ticker["symbol"]
            watchlist_symbols.add(symbol)
            lines.append(render_row(ticker.get("label", ""), symbol, history.get(symbol, []), flags_by_symbol))
        lines.append("")
    return "\n".join(lines), watchlist_symbols


def build_universe_section(history, flags_by_symbol, watchlist_symbols):
    cfg = load_yaml(CONFIG_DIR / "universe.yaml")
    lines = ["## universe 検出", ""]

    label_by_symbol = {}
    for theme in cfg.get("themes", []):
        for ticker in theme.get("tickers", []):
            label_by_symbol.setdefault(ticker["symbol"], ticker.get("label", ""))

    flagged_symbols = [
        symbol
        for symbol in label_by_symbol
        if symbol not in watchlist_symbols and symbol in flags_by_symbol
    ]

    if not flagged_symbols:
        lines.append("該当なし")
        lines.append("")
        return "\n".join(lines)

    lines.append("| label | symbol | 終値 | 前日比 | 週間騰落 | flags |")
    lines.append("|---|---|---|---|---|---|")
    for symbol in flagged_symbols:
        lines.append(render_row(label_by_symbol[symbol], symbol, history.get(symbol, []), flags_by_symbol))
    lines.append("")
    return "\n".join(lines)


def build_events_section(today):
    if not EVENTS_PATH.exists():
        return None  # Phase 3 未設定なら節ごと省略する

    cfg = load_yaml(EVENTS_PATH)
    events = cfg.get("events", [])

    lines = ["## 今後のイベント", ""]
    upcoming = []
    for event in events:
        try:
            event_date = datetime.strptime(str(event.get("date")), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        delta = (event_date - today).days
        if 0 <= delta <= EVENT_LOOKAHEAD_DAYS:
            upcoming.append((event_date, event))
    upcoming.sort(key=lambda pair: pair[0])

    if not upcoming:
        lines.append("該当なし")
        lines.append("")
        return "\n".join(lines)

    lines.append("| date | title | related |")
    lines.append("|---|---|---|")
    for event_date, event in upcoming:
        related = ", ".join(event.get("related", []) or [])
        lines.append(f"| {event_date.isoformat()} | {event.get('title', '')} | {related} |")
    lines.append("")
    return "\n".join(lines)


def build_failures_section():
    failures = load_json(FAILURES_PATH, [])
    lines = ["## 取得失敗", ""]
    if not failures:
        lines.append("該当なし")
    else:
        for symbol in failures:
            lines.append(f"- {symbol}")
    lines.append("")
    return "\n".join(lines)


def main():
    today = datetime.now(JST).date()
    history = load_history()
    flags_by_symbol = load_json(FLAGS_PATH, {})

    watchlist_section, watchlist_symbols = build_watchlist_section(history, flags_by_symbol)
    universe_section = build_universe_section(history, flags_by_symbol, watchlist_symbols)
    events_section = build_events_section(today)
    failures_section = build_failures_section()

    sections = [f"# stock-watch レポート {today.isoformat()}", "", watchlist_section, universe_section]
    if events_section is not None:
        sections.append(events_section)
    sections.append(failures_section)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{today.isoformat()}.md"
    out_path.write_text("\n".join(sections), encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
