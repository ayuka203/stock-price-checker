"""reports/YYYY-MM-DD.md（人が読む用）と reports/YYYY-MM-DD.json（grid-news 用）を生成する。

指標・フラグは screen.py が data/flags.json に出力したものを読む。
JSON のスキーマ（schema_version 1）は grid-news 側と合意済みなので、キーや型を変えない。
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
FLAGS_PATH = DATA_DIR / "flags.json"
FAILURES_PATH = DATA_DIR / "fetch_failures.json"
STATE_PATH = DATA_DIR / "asof_state.json"
EVENTS_PATH = CONFIG_DIR / "events.yaml"

JST = ZoneInfo("Asia/Tokyo")

SCHEMA_VERSION = 1
MARKETS = ("JP", "US")

# events.yaml のうち実行日からこの日数以内のものだけをレポートに列挙する
EVENT_LOOKAHEAD_DAYS = 14

# movers: |z| がこの値以上の銘柄を、絶対値の降順で最大 MAX_MOVERS 件
MOVER_MIN_ABS_Z = 2.0
MAX_MOVERS = 3

# headline: 全テーマの |平均騰落率| の最大がこの値未満なら「小動き」
QUIET_THRESHOLD = {"daily": 0.01, "weekly": 0.02}

FLAG_EMOJI = {
    "52w_high": "🏆",
    "52w_low": "🧊",
    "volume_spike": "🔵",
}


def load_yaml(path):
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path, default, expected_type):
    """JSON を読む。無い・壊れている・型が違う場合は警告して default を返す。"""
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:  # JSONDecodeError / UnicodeDecodeError は ValueError 系
        print(f"warning: cannot read {path.name}: {exc}; using default", file=sys.stderr)
        return default
    if not isinstance(data, expected_type):
        print(
            f"warning: {path.name} should be {expected_type.__name__}, got {type(data).__name__}; using default",
            file=sys.stderr,
        )
        return default
    return data


def write_text_atomic(path, text):
    """一時ファイルに書いてから置き換える（書き込み途中の中断でファイルを壊さない）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def dumps_json(data):
    # NaN / Infinity は JSON として不正なので、混入したら ValueError で止める
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def is_business_day(report_date):
    """JST の土日以外。"""
    return report_date.weekday() < 5


def market_of(symbol):
    return "JP" if symbol.endswith(".T") else "US"


def edition_of(report_date):
    """JST で月曜なら weekly、それ以外は daily。"""
    return "weekly" if report_date.weekday() == 0 else "daily"


def finite_or_none(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def rnd(value, digits):
    value = finite_or_none(value)
    return None if value is None else round(value, digits)


def fmt_price(value):
    return f"{value:.1f}" if value is not None else "—"


def fmt_pct(value):
    """小数の騰落率を +x.x% 表記にする。丸めて 0 になる値は -0.0% でなく +0.0% にする。"""
    if value is None:
        return "—"
    return f"{round(value * 100, 1) + 0.0:+.1f}%"


def fmt_z(value):
    return f"{value:+.1f}" if value is not None else "—"


def average(values):
    values = [v for v in values if finite_or_none(v) is not None]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


# ---------------------------------------------------------------- 構成の組み立て

def collect_tickers(watch_cfg, universe_cfg):
    """watchlist → universe の順に、重複を watchlist 優先で 1 つにした銘柄一覧を返す。

    各要素は {symbol, label, theme, layer, keywords}。
    """
    tickers = []
    seen = set()
    for layer, cfg in (("watchlist", watch_cfg), ("universe", universe_cfg)):
        for theme in cfg.get("themes", []):
            for ticker in theme.get("tickers", []):
                symbol = ticker["symbol"]
                if symbol in seen:
                    continue
                seen.add(symbol)
                tickers.append(
                    {
                        "symbol": symbol,
                        "label": ticker.get("label", ""),
                        "theme": theme.get("name", ""),
                        "layer": layer,
                        "keywords": list(ticker.get("keywords") or []),
                    }
                )
    return tickers


def resolve_asof(entries):
    """市場ごとの基準日 = その市場の銘柄の最新日付の最大値。銘柄が無ければ None。"""
    asof = {}
    for market in MARKETS:
        dates = [e["asof"] for e in entries if e["market"] == market and e["asof"]]
        asof[market] = max(dates) if dates else None
    return asof


def resolve_stale(asof, state, report_date):
    """前回実行時の市場 asof から日付が進んでいなければ stale。

    同じ report_date の再実行では、その日の最初の実行前の asof（prev_asof）と
    比較する（再実行で stale 扱いにならないように）。状態ファイルが無ければ false。
    戻り値は ({"JP_stale", "US_stale"}, 新しい状態)。
    """
    state = state or {}
    if state.get("report_date") == report_date:
        base = state.get("prev_asof") or {}
    else:
        base = state.get("asof") or {}
    old_asof = state.get("asof") or {}

    stale = {}
    new_asof = {}
    for market in MARKETS:
        prev = base.get(market)
        current = asof[market]
        stale[f"{market}_stale"] = prev is not None and (current is None or current <= prev)
        new_asof[market] = current if current is not None else old_asof.get(market)
    new_state = {
        "report_date": report_date,
        "asof": new_asof,
        "prev_asof": {market: base.get(market) for market in MARKETS},
    }
    return stale, new_state


def build_themes(watch_cfg, universe_cfg, ticker_values):
    """全テーマについて構成銘柄の単純平均（None 除外）を出す。

    ticker_values は {symbol: {"daily_pct", "weekly_pct", ...}}。重複銘柄は最初に現れた
    （= watchlist 側の）テーマにのみ数える。n は平均に使った銘柄数
    （前日比・週間で異なる場合は多い方）。
    """
    themes = []
    counted = set()
    for layer, cfg in (("watchlist", watch_cfg), ("universe", universe_cfg)):
        for theme in cfg.get("themes", []):
            symbols = []
            for ticker in theme.get("tickers", []):
                if ticker["symbol"] not in counted:
                    counted.add(ticker["symbol"])
                    symbols.append(ticker["symbol"])
            daily, n_daily = average([ticker_values[s]["daily_pct"] for s in symbols])
            weekly, n_weekly = average([ticker_values[s]["weekly_pct"] for s in symbols])
            themes.append(
                {
                    "name": theme.get("name", ""),
                    "layer": layer,
                    "daily_pct": daily,
                    "weekly_pct": weekly,
                    "n": max(n_daily, n_weekly),
                }
            )
    return themes


def make_headline(themes, edition):
    """テーマ別平均騰落率から決定的なテンプレート文を作る。"""
    key = "daily_pct" if edition == "daily" else "weekly_pct"
    valued = [(t["name"], t[key]) for t in themes if t[key] is not None]
    if not valued:
        return "株価データなし"

    prefix = "先週は" if edition == "weekly" else ""
    top_name, top = min(valued, key=lambda nv: (-nv[1], nv[0]))
    bottom_name, bottom = min(valued, key=lambda nv: (nv[1], nv[0]))

    if max(abs(v) for _, v in valued) < QUIET_THRESHOLD[edition]:
        body = "エネルギー関連は小動き"
    elif top > 0 and bottom < 0:
        body = f"{top_name}が上昇（平均{fmt_pct(top)}）、{bottom_name}は下落（平均{fmt_pct(bottom)}）"
    elif bottom >= 0:
        body = f"エネルギー関連は全面高（{top_name}が最大、平均{fmt_pct(top)}）"
    else:
        body = f"エネルギー関連は全面安（{bottom_name}が最大、平均{fmt_pct(bottom)}）"
    return prefix + body


def pick_movers(entries, edition):
    """|z|（weekly は週次 z）が閾値以上の銘柄を絶対値降順（同値は symbol 順）で上位 MAX_MOVERS 件。

    stale（銘柄の遅れ、または所属市場が前回から更新なし）の銘柄は、同じ動きの再掲を
    避けるため対象外。
    """
    z_key = "z" if edition == "daily" else "weekly_z"
    hits = [
        e
        for e in entries
        if not e["stale"] and e[z_key] is not None and abs(e[z_key]) >= MOVER_MIN_ABS_Z
    ]
    hits.sort(key=lambda e: (-abs(e[z_key]), e["symbol"]))
    movers = []
    for e in hits[:MAX_MOVERS]:
        movers.append(
            {
                "symbol": e["symbol"],
                "label": e["label"],
                "theme": e["theme"],
                "close": rnd(e["close"], 4),
                "daily_pct": rnd(e["daily_pct"], 5),
                "weekly_pct": rnd(e["weekly_pct"], 5),
                "z": rnd(e[z_key], 2),
                "flags": list(e["flags"]),
                "keywords": list(e["keywords"]),
            }
        )
    return movers


def build_report(report_date, watch_cfg, universe_cfg, macro_cfg, metrics, failures, state):
    """JSON レポート本体と、保存すべき新しい状態を返す。ネットワーク・ファイルに依存しない。

    新しい状態が None のときは状態ファイルを書き換えない（土日の実行）。

    metrics は screen.py の出力 {symbol: {date, close, daily_pct, weekly_pct, z, weekly_z, flags}}。
    """
    edition = edition_of(report_date)
    empty = {"date": None, "close": None, "daily_pct": None, "weekly_pct": None,
             "z": None, "weekly_z": None, "flags": []}

    entries = []
    for ticker in collect_tickers(watch_cfg, universe_cfg):
        m = {**empty, **metrics.get(ticker["symbol"], {})}
        entries.append(
            {
                **ticker,
                "market": market_of(ticker["symbol"]),
                "asof": m["date"],
                "close": finite_or_none(m["close"]),
                "daily_pct": finite_or_none(m["daily_pct"]),
                "weekly_pct": finite_or_none(m["weekly_pct"]),
                "z": finite_or_none(m["z"]),
                "weekly_z": finite_or_none(m["weekly_z"]),
                "flags": list(m["flags"] or []),
            }
        )

    asof = resolve_asof(entries)
    stale, new_state = resolve_stale(asof, state, report_date.isoformat())
    if not is_business_day(report_date):
        # 土日の手動実行で、金曜の asof を保存して月曜の定期実行を誤 stale にしない
        new_state = None

    # 銘柄の stale: 所属市場が前回から更新なし、または銘柄の最新日付が市場の基準日より古い
    # （日付が無い銘柄も、市場に基準日があれば古い側）
    for e in entries:
        market_asof = asof[e["market"]]
        behind = market_asof is not None and (e["asof"] is None or e["asof"] < market_asof)
        e["stale"] = stale[f"{e['market']}_stale"] or behind

    themes = build_themes(watch_cfg, universe_cfg, {e["symbol"]: e for e in entries})

    macro = []
    for item in macro_cfg.get("macro", []):
        m = {**empty, **metrics.get(item["symbol"], {})}
        macro.append(
            {
                "symbol": item["symbol"],
                "label": item.get("label", ""),
                "close": rnd(m["close"], 4),
                "daily_pct": rnd(m["daily_pct"], 5),
                "weekly_pct": rnd(m["weekly_pct"], 5),
                "asof": m["date"],
            }
        )

    tickers = []
    for e in entries:
        tickers.append(
            {
                "symbol": e["symbol"],
                "label": e["label"],
                "theme": e["theme"],
                "layer": e["layer"],
                "close": rnd(e["close"], 4),
                "daily_pct": rnd(e["daily_pct"], 5),
                "weekly_pct": rnd(e["weekly_pct"], 5),
                "z": rnd(e["z"], 2),
                "flags": e["flags"],
                "asof": e["asof"],
                "stale": e["stale"],
            }
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "report_date": report_date.isoformat(),
        "edition": edition,
        "asof": {"JP": asof["JP"], "US": asof["US"], **stale},
        "headline": make_headline(themes, edition),
        "themes": [
            {
                "name": t["name"],
                "layer": t["layer"],
                "daily_pct": rnd(t["daily_pct"], 5),
                "weekly_pct": rnd(t["weekly_pct"], 5),
                "n": t["n"],
            }
            for t in themes
        ],
        "movers": pick_movers(entries, edition),
        "macro": macro,
        "tickers": tickers,
        "fetch_failures": sorted(failures),
    }
    return report, new_state


# ---------------------------------------------------------------- Markdown

def flag_marks(flags, stale):
    marks = "".join(FLAG_EMOJI.get(f, "") for f in flags)
    return marks + ("⏸" if stale else "")


def render_ticker_table(rows):
    lines = [
        "| label | symbol | 終値 | 前日比 | 週間騰落 | z | flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in rows:
        lines.append(
            f"| {t['label']} | {t['symbol']} | {fmt_price(t['close'])} | "
            f"{fmt_pct(t['daily_pct'])} | {fmt_pct(t['weekly_pct'])} | {fmt_z(t['z'])} | "
            f"{flag_marks(t['flags'], t['stale'])} |"
        )
    return lines


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


def render_markdown(report, events_section):
    edition_label = "週次（月曜）" if report["edition"] == "weekly" else "日次"
    asof = report["asof"]
    parts = []
    for market in MARKETS:
        note = "（前回から更新なし）" if asof[f"{market}_stale"] else ""
        parts.append(f"{market} {asof[market] or '—'}{note}")

    lines = [
        f"# stock-watch レポート {report['report_date']}（{edition_label}）",
        "",
        f"**{report['headline']}**",
        "",
        "基準日: " + " / ".join(parts),
        "",
        "凡例: 🏆 52週高値更新（初回のみ） 🧊 52週安値更新（初回のみ） 🔵 出来高急増 ⏸ 前回から更新なし、または市場の基準日より古いデータ",
        "",
        "## テーマ別（銘柄単純平均）",
        "",
        "| テーマ | 前日比 | 週間騰落 | 銘柄数 |",
        "|---|---|---|---|",
    ]
    for t in report["themes"]:
        lines.append(f"| {t['name']} | {fmt_pct(t['daily_pct'])} | {fmt_pct(t['weekly_pct'])} | {t['n']} |")
    lines.append("")

    z_label = "z" if report["edition"] == "daily" else "週次z"
    lines += ["## 注目銘柄", ""]
    if not report["movers"]:
        lines += ["該当なし", ""]
    else:
        lines += [
            f"| label | symbol | テーマ | 終値 | 前日比 | 週間騰落 | {z_label} | flags |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for mv in report["movers"]:
            lines.append(
                f"| {mv['label']} | {mv['symbol']} | {mv['theme']} | {fmt_price(mv['close'])} | "
                f"{fmt_pct(mv['daily_pct'])} | {fmt_pct(mv['weekly_pct'])} | {fmt_z(mv['z'])} | "
                f"{flag_marks(mv['flags'], False)} |"
            )
        lines.append("")

    lines += ["## マクロ指標", "", "| 指標 | symbol | 終値 | 前日比 | 週間騰落 | 基準日 |", "|---|---|---|---|---|---|"]
    for m in report["macro"]:
        lines.append(
            f"| {m['label']} | {m['symbol']} | {fmt_price(m['close'])} | "
            f"{fmt_pct(m['daily_pct'])} | {fmt_pct(m['weekly_pct'])} | {m['asof'] or '—'} |"
        )
    lines.append("")

    lines += ["## 固定ウォッチ", ""]
    for theme in [t for t in report["themes"] if t["layer"] == "watchlist"]:
        rows = [t for t in report["tickers"] if t["layer"] == "watchlist" and t["theme"] == theme["name"]]
        lines += [f"### {theme['name']}", ""] + render_ticker_table(rows) + [""]

    mover_symbols = {mv["symbol"] for mv in report["movers"]}
    notable = [
        t
        for t in report["tickers"]
        if t["layer"] == "universe"
        and (t["flags"] or t["symbol"] in mover_symbols or (t["z"] is not None and abs(t["z"]) >= MOVER_MIN_ABS_Z))
    ]
    lines += ["## universe 検出", ""]
    if not notable:
        lines += ["該当なし", ""]
    else:
        lines += render_ticker_table(notable) + [""]

    if events_section is not None:
        lines += [events_section]

    lines += ["## 取得失敗", ""]
    if not report["fetch_failures"]:
        lines.append("該当なし")
    else:
        lines += [f"- {symbol}" for symbol in report["fetch_failures"]]
    lines.append("")
    return "\n".join(lines)


def main():
    today = datetime.now(JST).date()
    report, new_state = build_report(
        report_date=today,
        watch_cfg=load_yaml(CONFIG_DIR / "watchlist.yaml"),
        universe_cfg=load_yaml(CONFIG_DIR / "universe.yaml"),
        macro_cfg=load_yaml(CONFIG_DIR / "macro.yaml"),
        metrics=load_json(FLAGS_PATH, {}, dict),
        failures=load_json(FAILURES_PATH, [], list),
        state=load_json(STATE_PATH, None, dict),
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORTS_DIR / f"{today.isoformat()}.md"
    json_path = REPORTS_DIR / f"{today.isoformat()}.json"
    md_path.write_text(render_markdown(report, build_events_section(today)), encoding="utf-8")
    json_path.write_text(dumps_json(report), encoding="utf-8")

    if new_state is not None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        write_text_atomic(STATE_PATH, dumps_json(new_state))
    else:
        print("non-business day: asof_state.json left unchanged")
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")


if __name__ == "__main__":
    main()
