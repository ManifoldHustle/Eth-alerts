#!/usr/bin/env python3
"""
ETH setup alert bot (ALERTS ONLY - it never places orders).

Every run it:
  1. pulls closed 15m / 1h / 4h candles from a public feed (Kraken, Coinbase fallback)
  2. works out the trend (4h + 1h must agree), swing zones, a confirmation candle,
     volume, stop, targets and risk-to-reward
  3. sends a Telegram message ONLY when a full setup is confirmed on a closed 15m candle

Rules encoded (from the trading rules we agreed on):
  - trade WITH the 4h and 1h trend only, never counter-trend
  - levels are swing zones that already caused a reaction (or a broken level that held)
  - entry only after a candle CLOSES back out of the zone in the trade direction (no wick-only signals)
  - stop = beyond the zone / wick by 0.5 x ATR(1h)
  - targets = printed swing highs/lows, stopped a little short (0.2 x ATR) in front
  - alert only if risk-to-reward >= MIN_RR and the stop is not too wide for the leverage
  - size = balance x risk% / stop distance

Config comes from environment variables (see README).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone.utc

# ----------------------------- config -----------------------------------
def _env(name, default):
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


BALANCE = float(_env("BALANCE", "83.95"))        # account balance in USDT (update it as it changes)
RISK_PCT = float(_env("RISK_PCT", "7"))          # % of balance risked per trade
LEVERAGE = float(_env("LEVERAGE", "20"))
MIN_RR = float(_env("MIN_RR", "1.9"))            # minimum blended reward:risk
SOURCE = _env("SOURCE", "kraken").lower()        # kraken | coinbase
SL_ATR_MULT = float(_env("SL_ATR_MULT", "0.5"))  # stop buffer beyond the zone/wick, in 1h ATRs
VOL_REQUIRED = _env("VOL_REQUIRED", "1") == "1"  # confirmation candle volume must be >= its 20-candle average
BTC_FILTER = _env("BTC_FILTER", "1") == "1"      # skip longs when BTC 1h trend is down (and shorts when up)
NEAR_MISS = _env("NEAR_MISS", "1") == "1"        # send "near miss" notes (clearly marked, not a setup)
NEAR_RR_FLOOR = float(_env("NEAR_RR_FLOOR", "1.5"))
TRADE_TXT = _env("TRADE", "")                    # your open trade, e.g. "long 2470 2443 2511 2574 2455"
PAUSE_IN_TRADE = _env("PAUSE_IN_TRADE", "1") == "1"  # no new setup alerts while a trade is being watched
TIME_STOP_H = float(_env("TIME_STOP_H", "48"))
TP1_PORTION = min(max(float(_env("TP1_PORTION", "0.3333333333")), 0.1), 1.0)  # share of the position closed at TP1 (1 = single TP)
TP2_R = float(_env("TP2_R", "0"))                # >0: put TP2 at this many R (risk multiples) instead of a far level
MAX_ATR_PCT = float(_env("MAX_ATR_PCT", "0"))    # >0: skip setups when 1h ATR is this % of price or more
MAX_VOL_RATIO = float(_env("MAX_VOL_RATIO", "0"))  # >0: skip setups when the confirmation candle's volume is above this x its average
FLIPPED_ONLY = _env("FLIPPED_ONLY", "0") == "1"  # 1: only trade zones that were broken and flipped (skip plain swing highs/lows)
SINGLE_TP = TP1_PORTION >= 0.999
ASSET = _env("ASSET", "ETH").upper()             # backtest only: which Coinbase USD market to test (ETH, BTC, SUI, ...)
DP = 2                                           # price decimals (set from the price level when testing cheap coins)
ZSTEP = 1.0                                      # size of a "same zone" bucket for de-duplicating alerts
RUN_WINDOW_MIN = int(_env("RUN_WINDOW_MIN", "35"))  # only alert on a candle that closed recently
STATE_FILE = _env("STATE_FILE", "state.json")
LOG_FILE = _env("LOG_FILE", "alerts_log.json")      # every alert + what happened to it      # remembers alerts already sent (no duplicates)
DRY_RUN = _env("DRY_RUN", "0") == "1"            # print instead of sending
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
HEARTBEAT_HOUR_UTC = int(_env("HEARTBEAT_HOUR_UTC", "13"))  # daily "I'm alive" (13 UTC = 9am ET)

SEC = {"15m": 900, "1h": 3600, "4h": 14400}


# ----------------------------- data -------------------------------------
def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "eth-alert-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_kraken(tf, pair="ETHUSD"):
    interval = {"15m": 15, "1h": 60, "4h": 240}[tf]
    d = http_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}")
    if d.get("error"):
        raise RuntimeError(f"kraken error {d['error']}")
    key = [k for k in d["result"] if k != "last"][0]
    out = []
    for row in d["result"][key]:
        out.append({"t": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                    "l": float(row[3]), "c": float(row[4]), "v": float(row[6])})
    return out


def fetch_coinbase_raw(gran, product="ETH-USD"):
    rows = http_json(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity={gran}")
    out = []
    for t, low, high, op, cl, vol in rows:  # newest first
        out.append({"t": int(t), "o": float(op), "h": float(high),
                    "l": float(low), "c": float(cl), "v": float(vol)})
    out.sort(key=lambda x: x["t"])
    return out


def aggregate(cs, period):
    """Group smaller candles into `period`-second candles aligned to UTC."""
    buckets = {}
    for c in cs:
        b = c["t"] - c["t"] % period
        if b not in buckets:
            buckets[b] = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
        else:
            x = buckets[b]
            x["h"] = max(x["h"], c["h"])
            x["l"] = min(x["l"], c["l"])
            x["c"] = c["c"]
            x["v"] += c["v"]
    return [buckets[k] for k in sorted(buckets)]


def fetch_coinbase(tf, product="ETH-USD"):
    if tf == "15m":
        return fetch_coinbase_raw(900, product)
    if tf == "1h":
        return fetch_coinbase_raw(3600, product)
    return aggregate(fetch_coinbase_raw(3600, product), 14400)


def drop_unfinished(cs, tf, now):
    return [c for c in cs if c["t"] + SEC[tf] <= now]


def load_all(now):
    data = {}
    for tf in ("15m", "1h", "4h"):
        cs = None
        order = [SOURCE] + [s for s in ("kraken", "coinbase") if s != SOURCE]
        last_err = None
        for src in order:
            try:
                cs = fetch_kraken(tf) if src == "kraken" else fetch_coinbase(tf)
                break
            except Exception as e:  # try next source
                last_err = e
        if cs is None:
            raise RuntimeError(f"no data for {tf}: {last_err}")
        data[tf] = drop_unfinished(cs, tf, now)
    # BTC 1h trend for the market filter (non-fatal if unavailable)
    data["btc1h"] = None
    if BTC_FILTER:
        for src in ("kraken", "coinbase"):
            try:
                cs = fetch_kraken("1h", "XBTUSD") if src == "kraken" else fetch_coinbase("1h", "BTC-USD")
                data["btc1h"] = drop_unfinished(cs, "1h", now)
                break
            except Exception:
                continue
    return data


# --------------------------- indicators ---------------------------------
def sma(vals, n):
    out = []
    for i in range(len(vals)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(vals[i + 1 - n:i + 1]) / n)
    return out


def ema(vals, n):
    k = 2 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def macd_hist(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    return [a - b for a, b in zip(dif, dea)]


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(cs, n=14):
    trs = []
    for i in range(1, len(cs)):
        pc = cs[i - 1]["c"]
        trs.append(max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - pc), abs(cs[i]["l"] - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def trend(cs):
    """'bull' if MACD histogram > 0 and close above MA20; 'bear' mirror; else 'mixed'."""
    closes = [c["c"] for c in cs]
    if len(closes) < 40:
        return "mixed"
    h = macd_hist(closes)[-1]
    ma = sma(closes, 20)[-1]
    if h > 0 and closes[-1] > ma:
        return "bull"
    if h < 0 and closes[-1] < ma:
        return "bear"
    return "mixed"


def pivots(cs, k):
    highs, lows = [], []
    for i in range(k, len(cs) - k):
        h, l = cs[i]["h"], cs[i]["l"]
        if all(h > cs[j]["h"] for j in range(i - k, i + k + 1) if j != i):
            highs.append((i, h))
        if all(l < cs[j]["l"] for j in range(i - k, i + k + 1) if j != i):
            lows.append((i, l))
    return highs, lows


# ----------------------------- mirror -----------------------------------
def mirror(cs):
    """Flip prices so a SHORT setup can reuse the LONG logic."""
    return [{"t": c["t"], "o": -c["o"], "h": -c["l"], "l": -c["h"], "c": -c["c"], "v": c["v"]} for c in cs]


# ------------------------- long setup finder ----------------------------
def find_long(c15, c1h, c4h):
    """Return (full_setup, near_miss, notes) for the last closed 15m candle (LONG logic).
    full_setup: everything passed. near_miss: only soft checks failed (ratio a bit low, volume low,
    stop a bit wide). Hard failures (trend, zone, candle, no target) return nothing."""
    notes = []
    if len(c15) < 60 or len(c1h) < 60 or len(c4h) < 40:
        return None, None, ["not enough candles"]
    t4, t1 = trend(c4h), trend(c1h)
    if not (t4 == "bull" and t1 == "bull"):
        return None, None, [f"trend not aligned (4h {t4}, 1h {t1})"]

    a1 = atr(c1h)
    if not a1:
        return None, None, ["no ATR"]
    last = c15[-1]
    window_hi = c1h[-150:]

    # ---- candidate support zones ----
    levels = []  # (level, label)
    hi1, lo1 = pivots(window_hi, 3)
    for i, lv in lo1:
        after_hi = max(c["h"] for c in window_hi[i + 1:]) if i + 1 < len(window_hi) else lv
        if after_hi - lv >= 1.5 * a1 and lv < last["c"]:
            levels.append((lv, "1h swing low"))
    for i, lv in hi1:  # broken highs that flipped to support
        broke = None
        for j in range(i + 1, len(window_hi)):
            if window_hi[j]["c"] > lv + 0.1 * a1:
                broke = j
                break
        if broke is None:
            continue
        if any(window_hi[j]["c"] < lv - 0.3 * a1 for j in range(broke, len(window_hi))):
            continue
        levels.append((lv, "broken 1h high (now support)"))

    # ---- confirmation candle on the last closed 15m ----
    body = last["c"] - last["o"]
    rng = max(last["h"] - last["l"], 1e-9)
    vol_ma = sma([c["v"] for c in c15], 20)[-1]
    vol_ok = vol_ma is not None and last["v"] >= vol_ma

    full, near = None, None
    for lv, label in sorted(levels, key=lambda x: -x[0]):
        zone_top = lv + 0.6 * a1
        touched = last["l"] <= zone_top and last["l"] >= lv - 0.5 * a1
        closed_out = last["c"] > zone_top and body > 0 and body / rng >= 0.35
        if not (touched and closed_out):
            continue

        entry = last["c"]
        sl = min(lv, last["l"]) - SL_ATR_MULT * a1
        risk = entry - sl
        if risk < 0.3 * a1:
            notes.append(f"{label} {lv:.2f}: stop too tight")
            continue
        issues = []
        cap = abs(entry) * (0.5 / LEVERAGE)
        if risk > cap * 1.3:
            notes.append(f"{label} {lv:.2f}: stop far too wide for {LEVERAGE:.0f}x")
            continue
        if risk > cap:
            issues.append(f"stop {risk:.1f} pts is wider than the {cap:.1f} pt limit for {LEVERAGE:.0f}x")

        # ---- targets: printed swing highs above entry ----
        buf = 0.2 * a1
        tgt = set()
        for _, h in pivots(c1h[-150:], 3)[0]:
            tgt.add(round(h, DP))
        for _, h in pivots(c4h[-120:], 2)[0]:
            tgt.add(round(h, DP))
        tgt.add(round(max(c["h"] for c in c15[-96:]), 2))  # 24h high
        ups = sorted(x for x in tgt if x - buf > entry)
        min_dist = max(0.8, MIN_RR) if SINGLE_TP else 0.8  # single TP: nearest level that already gives your ratio
        tp1 = next((x - buf for x in ups if x - buf - entry >= min_dist * risk), None)
        if tp1 is None:
            notes.append(f"{label} {lv:.2f}: no target far enough")
            continue
        if SINGLE_TP:
            tp2 = None
        elif TP2_R > 0:
            tp2 = entry + TP2_R * risk
            if tp2 <= tp1 + 0.2 * a1:
                tp2 = None
        else:
            tp2 = next((x - buf for x in ups if x - buf >= tp1 + 1.5 * a1), None)
        if tp2:
            w = (TP1_PORTION, 1 - TP1_PORTION)
            rr = (w[0] * (tp1 - entry) + w[1] * (tp2 - entry)) / risk
        else:
            rr = (tp1 - entry) / risk
            w = (1.0, 0.0)
        if rr < MIN_RR:
            if rr >= NEAR_RR_FLOOR:
                issues.append(f"R:R {rr:.2f} is below your minimum {MIN_RR}")
            else:
                notes.append(f"{label} {lv:.2f}: R:R {rr:.2f} too low")
                continue
        if VOL_REQUIRED and not vol_ok:
            issues.append("volume on the confirmation candle is below its 20-candle average")

        score = 4 + (1 if vol_ok else 0)
        cand = {"level": lv, "label": label, "zone_top": zone_top, "entry": entry, "sl": sl,
                "risk": risk, "tp1": tp1, "tp2": tp2, "w": w, "rr": rr, "score": score,
                "vol_ok": vol_ok, "atr1h": a1, "t4": t4, "t1": t1, "issues": issues,
                "candle_close_t": last["t"] + SEC["15m"]}
        if not issues:
            if full is None or cand["rr"] > full["rr"]:
                full = cand
        else:
            if near is None or cand["rr"] > near["rr"]:
                near = cand
    return full, near, notes


def to_short(s):
    """Convert a mirrored long setup back to real prices."""
    if s is None:
        return None
    o = dict(s)
    for k in ("level", "zone_top", "entry", "sl", "tp1"):
        o[k] = -s[k]
    o["tp2"] = -s["tp2"] if s["tp2"] else None
    o["t4"], o["t1"] = "bear", "bear"
    o["label"] = (s["label"].replace("broken 1h high (now support)", "broken 1h low (now resistance)")
                  .replace("1h swing low", "1h swing high"))
    return o


# ----------------------------- messages ---------------------------------
def fmt_t(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%a %b %d %H:%M ET")


def build_message(side, s):
    size = BALANCE * RISK_PCT / 100 / s["risk"]
    margin = size * s["entry"] / LEVERAGE
    capped = ""
    if margin > BALANCE * 0.9:  # never let the plan need more margin than the account has
        size = BALANCE * 0.9 * LEVERAGE / s["entry"]
        margin = size * s["entry"] / LEVERAGE
        capped = "  (size capped by available margin, so real risk is lower)"
    risk_usd = size * s["risk"]
    arrow = "🟢 LONG" if side == "long" else "🔴 SHORT"
    kind = s.get("kind", "full")
    sgn = 1 if side == "long" else -1
    p = lambda x: f"{x:,.{DP}f}"
    lines = [
        (f"{arrow} setup confirmed  ({s['score']}/5)  {fmt_t(s['candle_close_t'])}" if kind == "full"
         else f"🟡 NEAR MISS, NOT a full setup ({side})  {fmt_t(s['candle_close_t'])}"),
        f"Trend: 4h {s['t4']}, 1h {s['t1']}",
        f"Zone: {s['label']} {p(s['level'])} (zone edge {p(s['zone_top'])})",
        f"Candle: 15m closed out of the zone"
        f"{'  |  volume above average' if s['vol_ok'] else '  |  volume BELOW average'}",
        *( ["Why it missed: " + "; ".join(s["issues"]), "Numbers below are what it WOULD be. Your call; send me screenshots.", ""]
           if kind == "near" else [] ),
        "",
        f"SIZE: {size:.3f} ETH  @ {LEVERAGE:.0f}x Isolated  (margin ≈ ${margin:.2f})",
        f"ENTRY: ~{p(s['entry'])} (market at this close)",
        f"SL: {p(s['sl'])}  (risk ${risk_usd:.2f} = {risk_usd / BALANCE * 100:.1f}% of ${BALANCE:.2f}){capped}",
    ]
    if s["tp2"]:
        lines.append(f"TP1: {p(s['tp1'])}  close {size * TP1_PORTION:.3f} ETH ({TP1_PORTION * 100:.0f}%), then move SL to entry")
        lines.append(f"TP2: {p(s['tp2'])}  close {size * (1 - TP1_PORTION):.3f} ETH")
    else:
        lines.append(f"TP: {p(s['tp1'])}  close all {size:.3f} ETH")
    lines += [
        f"R:R ≈ {s['rr']:.2f}:1",
        "",
        f"Skip it if price has already moved more than {0.25 * s['atr1h']:.1f} points from the entry "
        f"({'above' if side == 'long' else 'below'}).",
        f"Cancel the idea if a 1h candle closes {'below' if side == 'long' else 'above'} {p(s['sl'])}.",
        f"Time stop: close at market if TP1 isn't hit within {TIME_STOP_H:.0f}h.",
        "Send me the 15m/1h/4h screenshots to double-check before you place it. Not financial advice.",
    ]
    return "\n".join(lines)


def build_status(data):
    c15, c1h, c4h = data["15m"], data["1h"], data["4h"]
    a1 = atr(c1h)
    r1 = rsi([c["c"] for c in c1h])
    extra = ""
    try:
        t = parse_trade(TRADE_TXT)
        if t:
            extra = f" Watching your {t['side']} from {t['entry']:,.2f} (SL {t['sl']:,.2f}, TP1 {t['tp1']:,.2f})."
    except Exception:
        pass
    return (f"Bot alive. ETH {c15[-1]['c']:,.2f} | 4h {trend(c4h)} | 1h {trend(c1h)} | "
            f"1h RSI {r1:.0f} | 1h ATR {a1:.1f}." + (extra or " Watching for setups."))


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"sent": []}


def save_state(st):
    st["sent"] = st["sent"][-60:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def send(text):
    if DRY_RUN or not (TG_TOKEN and TG_CHAT):
        print("---- (not sent: DRY_RUN or no Telegram secrets) ----")
        print(text)
        return
    body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    print("sent")




# ----------------------------- alert log + scorecard ---------------------
def load_log():
    try:
        with open(LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerts": []}


def save_log(lg):
    with open(LOG_FILE, "w") as f:
        json.dump(lg, f, indent=1)


def log_alert(lg, side, s, kind, now):
    """Record an alert so we can later check what price did afterwards."""
    aid = f"{kind}:{side}:{s['candle_close_t']}"
    if any(a["id"] == aid for a in lg["alerts"]):
        return
    lg["alerts"].append({
        "id": aid, "kind": kind, "side": side, "t0": s["candle_close_t"],
        "label": s["label"], "level": round(s["level"], DP), "entry": round(s["entry"], DP),
        "sl": round(s["sl"], DP), "tp1": round(s["tp1"], DP),
        "tp2": round(s["tp2"], DP) if s["tp2"] else None, "risk": round(s["risk"], DP),
        "rr": round(s["rr"], 2), "score": s["score"], "issues": s.get("issues", []),
        "stage": "open", "result": None, "R": None, "closed_t": None})


def resolve_open(lg, data, now, send_fn=None):
    """Replay candles after each open alert: stop / TP1 / TP2 / time stop (stop checked first in a candle)."""
    send_fn = send_fn or send
    c15 = data["15m"]
    for a in lg["alerts"]:
        if a["result"]:
            continue
        sg = 1 if a["side"] == "long" else -1
        entry, sl, tp1, tp2, risk = a["entry"], a["sl"], a["tp1"], a["tp2"], a["risk"]
        w1, w2 = (TP1_PORTION, 1 - TP1_PORTION) if tp2 else (1.0, 0.0)
        r_tp1 = (tp1 - entry) * sg / risk
        r_tp2 = (tp2 - entry) * sg / risk if tp2 else None
        res = None
        for c in c15:
            if c["t"] < a["t0"]:
                continue
            worst = c["l"] if sg == 1 else c["h"]
            best = c["h"] if sg == 1 else c["l"]
            sl_now = entry if a["stage"] == "tp1" else sl
            if (worst - sl_now) * sg <= 0:
                res = ("SL", -1.0) if a["stage"] == "open" else ("TP1 then breakeven", w1 * r_tp1)
                a["closed_t"] = c["t"] + SEC["15m"]
                break
            if a["stage"] == "open" and (best - tp1) * sg >= 0:
                if not tp2:
                    res = ("TP", r_tp1)
                    a["closed_t"] = c["t"] + SEC["15m"]
                    break
                a["stage"] = "tp1"
            if a["stage"] == "tp1" and tp2 and (best - tp2) * sg >= 0:
                res = ("TP1 and TP2", w1 * r_tp1 + w2 * r_tp2)
                a["closed_t"] = c["t"] + SEC["15m"]
                break
        if not res and (now - a["t0"]) >= TIME_STOP_H * 3600:
            last = c15[-1]["c"]
            r_open = (last - entry) * sg / risk
            res = (("Time stop (TP1 hit)", w1 * r_tp1 + w2 * r_open) if a["stage"] == "tp1"
                   else ("Time stop", r_open))
            a["closed_t"] = now
        if res:
            a["result"], a["R"] = res[0], round(res[1], 2)
            tag = "NEAR MISS" if a["kind"] == "near" else "alert"
            send_fn(f"📒 RESULT of the {a['side']} {tag} from {fmt_t(a['t0'])} (entry {a['entry']:,.2f}): "
                    f"{a['result']}, {a['R']:+.2f}R  (1R = the amount risked; -1R = stopped out)")


def scorecard(lg):
    rows = lg["alerts"]
    if not rows:
        return "Scorecard: no alerts logged yet."
    out = ["📊 SCORECARD  (R = multiples of the amount risked; -1R = a full stop-out)"]
    for kind, title in (("full", "FULL alerts"), ("near", "NEAR MISSES (not full setups)")):
        sub = [a for a in rows if a["kind"] == kind]
        if not sub:
            continue
        done = [a for a in sub if a["result"]]
        openn = len(sub) - len(done)
        wins = [a for a in done if a["R"] > 0]
        tot = sum(a["R"] for a in done)
        line = f"{title}: {len(sub)} logged, {len(done)} finished ({len(wins)} won, {len(done) - len(wins)} lost/flat)"
        if done:
            line += f", total {tot:+.2f}R, average {tot / len(done):+.2f}R"
        if openn:
            line += f", {openn} still open"
        out.append(line)
        for side in ("long", "short"):
            d = [a for a in done if a["side"] == side]
            if d:
                out.append(f"   {side}: {len(d)} finished, {sum(a['R'] for a in d):+.2f}R")
    out.append("Small samples mislead. Don't change a rule until there are about 15-20 finished alerts.")
    return "\n".join(out)

# --------------------------- trade watcher -------------------------------
def parse_trade(txt):
    """'long 2470 2443 2511 2574 2455' = side entry sl tp1 [tp2] [invalid]. Use 0 to skip tp2/invalid."""
    if not txt or not txt.strip():
        return None
    parts = [p for p in re.split(r"[\s,;]+", txt.strip().lower()) if p]
    if not parts or parts[0] in ("none", "off", "-", "0"):
        return None
    side = parts[0]
    if side not in ("long", "short"):
        raise ValueError("first word must be long or short")
    nums = [float(x) for x in parts[1:]]
    if len(nums) < 3:
        raise ValueError("need: side entry sl tp1 [tp2] [invalid]")
    entry, sl, tp1 = nums[0], nums[1], nums[2]
    tp2 = nums[3] if len(nums) > 3 and nums[3] > 0 else None
    inv = nums[4] if len(nums) > 4 and nums[4] > 0 else None
    sg = 1 if side == "long" else -1
    if not (sl * sg < entry * sg < tp1 * sg):
        raise ValueError("levels are in the wrong order for a " + side)
    if tp2 is not None and not (tp2 * sg > tp1 * sg):
        raise ValueError("TP2 must be beyond TP1")
    return {"side": side, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "inv": inv,
            "raw": f"{side} {entry} {sl} {tp1} {tp2} {inv}"}


def watch_trade(data, now, st, tr_in, send_fn=None):
    """Watches an open trade. Returns True while the trade is still open (alerts paused)."""
    send_fn = send_fn or send
    tr = st.setdefault("trade", {})
    if tr.get("key") != tr_in["raw"]:
        tr.clear()
        tr.update({"key": tr_in["raw"], "first_seen": now, "done": []})
    done = tr["done"]
    if "closed" in done:
        return False
    sg = 1 if tr_in["side"] == "long" else -1
    _against = "bear" if sg == 1 else "bull"
    if "flip0" not in tr:
        tr["flip0"] = trend(data["4h"]) == _against and trend(data["1h"]) == _against
    entry, sl, tp1, tp2, inv = (tr_in[k] for k in ("entry", "sl", "tp1", "tp2", "inv"))
    side = tr_in["side"].upper()
    c15, c1h, c4h = data["15m"], data["1h"], data["4h"]
    a1 = atr(c1h) or 0
    p = lambda x: f"{x:,.{DP}f}"

    def say(key, text):
        if key in done:
            return
        send_fn(f"📍 TRADE WATCH ({side} from {p(entry)})\n{text}")
        done.append(key)

    since = [c for c in c15 if c["t"] + SEC["15m"] > tr["first_seen"]]
    for c in since:
        worst = c["l"] if sg == 1 else c["h"]      # most adverse price in the candle
        best = c["h"] if sg == 1 else c["l"]       # most favourable price in the candle
        sl_now = entry if "tp1" in done else sl   # after TP1 you move the SL to entry
        if (worst - sl_now) * sg <= 0:
            what = "your entry (the breakeven stop)" if "tp1" in done else f"your stop {p(sl)}"
            say("sl", f"Price reached {what}. If WEEX closed the trade, it is over. "
                      f"Clear the TRADE variable (or set it to none) before the next one.")
            done.append("closed")
            return False
        if inv and (c["c"] - inv) * sg < 0 and "tp1" not in done:
            say("inv", f"INVALIDATED: a 15m candle closed beyond {p(inv)} (close {p(c['c'])}). "
                       f"Your rule says close at market now instead of waiting for the stop.")
        if (best - tp1) * sg >= 0 and "tp1" not in done:
            if tp2 is None:
                say("tp1", f"TP {p(tp1)} reached. Take the profit and clear the TRADE variable.")
                done.append("closed")
                return False
            say("tp1", f"TP1 {p(tp1)} reached. Close the first part and move your SL to entry {p(entry)}.")
        if tp2 is not None and (best - tp2) * sg >= 0 and "tp1" in done:
            say("tp2", f"TP2 {p(tp2)} reached. Close the rest and clear the TRADE variable.")
            done.append("closed")
            return False

    last = c15[-1]["c"]
    # price close to the stop (only before TP1)
    if "tp1" not in done and "sl" not in done:
        dist = (last - sl) * sg
        if 0 < dist <= 0.25 * abs(entry - sl):
            say("near", f"Price {p(last)} is within {dist:.1f} points of your stop {p(sl)}. Nothing to do unless you plan to exit early.")
    # time stop
    if "tp1" not in done and (now - tr["first_seen"]) / 3600 >= TIME_STOP_H:
        say("time", f"{TIME_STOP_H:.0f}h passed and TP1 hasn't hit. Time stop: close at market (price {p(last)}).")
    # both trends flipped against the trade
    against = "bear" if sg == 1 else "bull"
    if (not tr.get("flip0")) and trend(c4h) == against and trend(c1h) == against:
        say("flip", f"4h and 1h trends have both turned {against} against your {side.lower()}. Consider closing or tightening.")
    # optional trailing suggestion after TP1
    if "tp1" in done and a1:
        mc = mirror(c1h) if sg == -1 else c1h
        _, lows = pivots(mc, 3)
        lv = None
        for i, l in lows:
            if mc[i]["t"] >= tr["first_seen"]:
                lv = l if lv is None else max(lv, l)
        if lv is not None:
            level = sg * (lv - SL_ATR_MULT * a1)   # real-price trailing level (mirrored back for shorts)
            last_tr = tr.get("trail")
            if ((level - entry) * sg >= 0.3 * a1
                    and (last_tr is None or (level - last_tr) * sg >= 0.5 * a1)):
                send_fn(f"📍 TRADE WATCH ({side} from {p(entry)})\nOptional: trail your SL to {p(level)} "
                        f"(new swing {'low' if sg == 1 else 'high'} beyond your entry, plus buffer). Your call.")
                tr["trail"] = level
    return True


# ------------------------------- backtest --------------------------------
def fetch_coinbase_range(product, gran, start_ts, end_ts):
    """Page through Coinbase public candles (max 300 per request) and return oldest-first candles."""
    out, seen = [], set()
    step = 300 * gran
    t = start_ts
    while t < end_ts:
        t2 = min(t + step, end_ts)
        iso = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles"
               f"?granularity={gran}&start={iso(t)}&end={iso(t2)}")
        rows = None
        for attempt in range(4):
            try:
                rows = http_json(url)
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        if rows is None:
            raise RuntimeError(f"could not fetch {product} {gran}s candles")
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                out.append({"t": int(r[0]), "o": float(r[3]), "h": float(r[2]), "l": float(r[1]),
                            "c": float(r[4]), "v": float(r[5])})
        t = t2
        time.sleep(0.25)
    out.sort(key=lambda x: x["t"])
    return out


def backtest(c15_all, btc1h_all=None):
    """Replay the live rules candle by candle over history. Returns the alert log."""
    h1_all = aggregate(c15_all, 3600)
    h4_all = aggregate(c15_all, 14400)
    lg = {"alerts": []}
    last_zone = {}
    i1 = i4 = ib = 0
    t_first = None
    for end in range(60, len(c15_all) + 1):
        c15 = c15_all[max(0, end - 720):end]
        now = c15[-1]["t"] + SEC["15m"]
        while i1 < len(h1_all) and h1_all[i1]["t"] + 3600 <= now:
            i1 += 1
        while i4 < len(h4_all) and h4_all[i4]["t"] + 14400 <= now:
            i4 += 1
        c1h, c4h = h1_all[max(0, i1 - 720):i1], h4_all[max(0, i4 - 300):i4]
        btc = None
        if btc1h_all:
            while ib < len(btc1h_all) and btc1h_all[ib]["t"] + 3600 <= now:
                ib += 1
            btc = btc1h_all[max(0, ib - 300):ib]
        if len(c1h) < 200 or len(c4h) < 60:
            continue
        if t_first is None:
            t_first = c15[-1]["t"]
        data = {"15m": c15, "1h": c1h, "4h": c4h, "btc1h": btc}
        resolve_open(lg, data, now, send_fn=lambda m: None)
        res, _ = analyse(data)
        for side in ("long", "short"):
            full, near = res[side]
            for kind, sx in (("full", full), ("near", near if (NEAR_MISS and not full) else None)):
                if not sx:
                    continue
                zk = (kind, side, round(sx["level"] / ZSTEP))
                if zk in last_zone and now - last_zone[zk] < 4 * 3600:
                    continue
                last_zone[zk] = now
                n_before = len(lg["alerts"])
                log_alert(lg, side, sx, kind, now)
                if len(lg["alerts"]) > n_before:
                    lg["alerts"][-1]["f"] = alert_features(side, sx, c15, c1h, btc, now)
    lg["t_first"] = t_first or c15_all[0]["t"]
    lg["days"] = max(1.0, ((c15_all[-1]["t"] - lg["t_first"]) / 86400))
    return lg



def alert_features(side, sx, c15, c1h, btc, now):
    """Conditions at alert time, used only by the backtest breakdown."""
    f = {}
    try:
        et = datetime.fromtimestamp(now, ET)
        h = et.hour
        f["session"] = ("overnight 0-6 ET" if h < 6 else "morning 6-10 ET" if h < 10 else
                        "midday 10-14 ET" if h < 14 else "afternoon 14-18 ET" if h < 18 else "evening 18-24 ET")
        f["weekday"] = "weekend" if et.weekday() >= 5 else "weekday"
    except Exception:
        pass
    f["zone"] = sx["label"]
    f["trend 4h"] = sx.get("t4", "?")
    f["trend 1h"] = sx.get("t1", "?")
    a = atr(c1h)
    if a:
        pct = a / c15[-1]["c"] * 100
        f["volatility"] = "calm (ATR<0.6%)" if pct < 0.6 else "normal (0.6-0.9%)" if pct < 0.9 else "wild (ATR>0.9%)"
        f["stop size"] = ("tight (<0.6 ATR)" if sx["risk"] < 0.6 * a else "medium (0.6-1.0 ATR)" if sx["risk"] < 1.0 * a
                          else "wide (>1.0 ATR)")
    vols = [c["v"] for c in c15[-21:-1]]
    if vols and sum(vols) > 0:
        r = c15[-1]["v"] / (sum(vols) / len(vols))
        f["volume"] = "low (<1x avg)" if r < 1 else "ok (1-1.5x)" if r < 1.5 else "high (>1.5x avg)"
    if btc:
        bt = trend(btc)
        f["BTC 1h"] = ("with trade" if (bt == "bull" and side == "long") or (bt == "bear" and side == "short")
                       else "neutral" if bt == "mixed" else "against")
    f["ratio"] = "R:R 2.5-3" if sx["rr"] < 3 else "R:R 3+"
    f["tp2"] = "has TP2" if sx.get("tp2") else "TP1 only"
    return f


def _stats(g):
    if not g:
        return "no alerts"
    tp1 = sum(1 for x in g if x["result"] in ("TP", "TP1 and TP2", "TP1 then breakeven", "Time stop (TP1 hit)"))
    win = sum(x["R"] for x in g if x["R"] > 0)
    los = -sum(x["R"] for x in g if x["R"] <= 0)
    pf = f"{win / los:.2f}" if los > 0 else "n/a"
    return (f"{len(g)} alerts, {sum(x['R'] for x in g) / len(g):+.2f}R avg, total {sum(x['R'] for x in g):+.1f}R, "
            f"profit factor {pf}, TP1 {tp1 / len(g) * 100:.0f}%")


def half_report(lg):
    """Split the history: the first 2/3 vs the last 1/3. A rule only counts if it holds in both."""
    sub = [a for a in lg["alerts"] if a["kind"] == "full" and a["result"]]
    if not sub or "t_first" not in lg:
        return ""
    cut = lg["t_first"] + lg["days"] * 86400 * 2 / 3
    a, b = [x for x in sub if x["t0"] < cut], [x for x in sub if x["t0"] >= cut]
    few = len(a) < 15 or len(b) < 8
    ok = (not few) and sum(x["R"] for x in a) > 0 and sum(x["R"] for x in b) > 0
    t_end = lg["t_first"] + lg["days"] * 86400
    old = [x for x in sub if x["t0"] < t_end - 190 * 86400]
    old_txt = (f"\n   older than the last 190 days (data we had not looked at before): {_stats(old)}"
               if lg["days"] >= 250 else "")
    return ("\n🧭 SPLIT CHECK (full alerts)\n"
            f"   first {lg['days'] * 2 / 3:.0f} days: {_stats(a)}\n"
            f"   last {lg['days'] / 3:.0f} days: {_stats(b)}"
            f"{old_txt}\n"
            f"   {'Too few alerts in one part to judge (need 15+ and 8+).' if few else 'Positive in BOTH parts: worth a closer look.' if ok else 'NOT positive in both parts: do not trust it.'}")


def breakdown_report(lg, min_n=12):
    sub = [a for a in lg["alerts"] if a["kind"] == "full" and a["result"] and a.get("f")]
    if not sub:
        return "No full alerts to break down."
    out = [f"🔬 BREAKDOWN of {len(sub)} full alerts (groups with fewer than {min_n} alerts are skipped; average R per alert)"]
    keys = ["trend 4h", "trend 1h", "zone", "session", "weekday", "volatility", "stop size", "volume", "BTC 1h", "ratio", "tp2"]
    for k in keys:
        groups = {}
        for a in sub:
            v = a["f"].get(k)
            if v is not None:
                groups.setdefault(v, []).append(a)
        rows = []
        for v, g in sorted(groups.items(), key=lambda kv: -sum(x["R"] for x in kv[1]) / len(kv[1])):
            if len(g) < min_n:
                continue
            tp1 = sum(1 for x in g if x["result"] in ("TP", "TP1 and TP2", "TP1 then breakeven", "Time stop (TP1 hit)"))
            rows.append(f"   {v}: {len(g)} alerts, {sum(x['R'] for x in g) / len(g):+.2f}R avg, TP1 {tp1 / len(g) * 100:.0f}%")
        if rows:
            out.append(k.upper())
            out.extend(rows)
    return "\n".join(out)

def backtest_report(lg, label=""):
    days = lg.get("days", 1)
    out = [f"🧪 BACKTEST {label}  ({days:.0f} days of history, same rules as the live bot)"]
    for kind, title in (("full", "FULL alerts"), ("near", "NEAR MISSES")):
        sub = [a for a in lg["alerts"] if a["kind"] == kind and a["result"]]
        if not sub:
            out.append(f"{title}: none")
            continue
        n = len(sub)
        tp1 = [a for a in sub if a["result"] in ("TP", "TP1 and TP2", "TP1 then breakeven", "Time stop (TP1 hit)")]
        tp2 = [a for a in sub if a["result"] == "TP1 and TP2"]
        sl = [a for a in sub if a["result"] == "SL"]
        tm = [a for a in sub if a["result"].startswith("Time stop") and a not in tp1]
        wins = [a["R"] for a in sub if a["R"] > 0]
        loss = [a["R"] for a in sub if a["R"] <= 0]
        tot = sum(a["R"] for a in sub)
        pf = (sum(wins) / abs(sum(loss))) if loss and sum(loss) != 0 else float("inf")
        streak = cur = 0
        for a in sorted(sub, key=lambda a: a["t0"]):
            cur = cur + 1 if a["R"] <= 0 else 0
            streak = max(streak, cur)
        hrs = [((a["closed_t"] or a["t0"]) - a["t0"]) / 3600 for a in sub]
        out.append(f"{title}: {n} alerts = {n / days:.1f} per day ({n / days * 7:.1f} per week)")
        out.append(f"   reached TP1 {len(tp1) / n * 100:.0f}% | reached TP2 {len(tp2) / n * 100:.0f}% | "
                   f"hit SL first {len(sl) / n * 100:.0f}% | time stop only {len(tm) / n * 100:.0f}%")
        out.append(f"   average {tot / n:+.2f}R per alert, total {tot:+.1f}R, profit factor "
                   f"{('%.2f' % pf) if pf != float('inf') else 'n/a'}, longest losing streak {streak}, "
                   f"typical duration {sorted(hrs)[len(hrs) // 2]:.0f}h")
        for side in ("long", "short"):
            d = [a for a in sub if a["side"] == side]
            if d:
                out.append(f"   {side}: {len(d)} alerts, {sum(a['R'] for a in d):+.1f}R")
    out.append(f"Caveats: Coinbase {ASSET}-USD candles (not WEEX), alert-price fills, stop checked first inside a candle, "
               "no fees/slippage, overlapping alerts counted separately. A guide, not a promise.")
    return "\n".join(out)


def run_backtest(days):
    global DP, ZSTEP
    end_ts = int(time.time()) // 900 * 900
    start_ts = end_ts - int((days + 14) * 86400)
    print(f"fetching {days}+14 days of {ASSET} 15m candles...")
    eth = fetch_coinbase_range(f"{ASSET}-USD", 900, start_ts, end_ts)
    if len(eth) < 3000:
        raise RuntimeError(f"only {len(eth)} candles for {ASSET}-USD; check the symbol or try fewer days")
    px = eth[-1]["c"]
    DP = 2 if px >= 100 else (4 if px >= 1 else 6)
    ZSTEP = 1.0 if DP == 2 else px * 0.0004
    btc = None
    if BTC_FILTER and ASSET != "BTC":
        try:
            print("fetching BTC 1h candles...")
            btc = fetch_coinbase_range("BTC-USD", 3600, start_ts, end_ts)
        except Exception as e:
            print("BTC data unavailable, running without the BTC filter:", e)
    print(f"{len(eth)} {ASSET} candles; running...")
    lg = backtest(eth, btc)
    tp_txt = ("single TP" if SINGLE_TP else f"TP1 {TP1_PORTION * 100:.0f}%" + (f", TP2 at {TP2_R}R" if TP2_R > 0 else ""))
    flt = [x for x in (f"ATR<{MAX_ATR_PCT}%" if MAX_ATR_PCT > 0 else "", f"vol<={MAX_VOL_RATIO}x" if MAX_VOL_RATIO > 0 else "",
                       "flipped zones only" if FLIPPED_ONLY else "") if x]
    rep = backtest_report(lg, f"{ASSET} (SL buffer {SL_ATR_MULT} ATR, min R:R {MIN_RR}, {tp_txt}, time stop {TIME_STOP_H:.0f}h, "
                              f"vol {'on' if VOL_REQUIRED else 'off'}, BTC {'on' if btc else 'off'}"
                              f"{', filters: ' + ', '.join(flt) if flt else ''})")
    send(rep + half_report(lg))
    try:
        bd = breakdown_report(lg)
        for i in range(0, len(bd), 3800):
            send(bd[i:i + 3800])
    except Exception as e:
        print("breakdown failed:", e)


# ------------------------------- main -----------------------------------
def btc_blocks(side, data):
    """BTC 1h trend against the trade blocks it (fail-open if BTC data is unavailable)."""
    if not BTC_FILTER or not data.get("btc1h"):
        return None
    t = trend(data["btc1h"])
    if side == "long" and t == "bear":
        return "BTC 1h trend is down"
    if side == "short" and t == "bull":
        return "BTC 1h trend is up"
    return None


def filter_reason(sx, c15, c1h):
    """Optional filters (all off by default). Returns why a setup is skipped, or None."""
    if sx is None:
        return None
    if FLIPPED_ONLY and not sx["label"].startswith("broken"):
        return "plain swing level (FLIPPED_ONLY is on)"
    if MAX_ATR_PCT > 0:
        a = atr(c1h)
        if a and a / c15[-1]["c"] * 100 >= MAX_ATR_PCT:
            return "volatility too high"
    if MAX_VOL_RATIO > 0:
        vols = [c["v"] for c in c15[-21:-1]]
        if vols and sum(vols) > 0 and c15[-1]["v"] / (sum(vols) / len(vols)) > MAX_VOL_RATIO:
            return "confirmation volume too high"
    return None


def analyse(data):
    c15, c1h, c4h = data["15m"], data["1h"], data["4h"]
    lf, ln, n1 = find_long(c15, c1h, c4h)
    sf, sn, n2 = find_long(mirror(c15), mirror(c1h), mirror(c4h))
    out = {"long": (lf, ln), "short": (to_short(sf), to_short(sn))}
    for side in ("long", "short"):
        full, near = out[side]
        if filter_reason(full, c15, c1h):
            full = None
        if filter_reason(near, c15, c1h):
            near = None
        why = btc_blocks(side, data)
        if why and full:
            full["issues"] = full["issues"] + [why]
            near, full = (full if not near or full["rr"] > near["rr"] else near), None
        elif why and near:
            near["issues"] = near["issues"] + [why]
        out[side] = (full, near)
    notes = [f"[long] {n}" for n in n1] + [f"[short, mirrored view] {n}" for n in n2]
    return out, notes


def main():
    args = sys.argv[1:]
    now = int(time.time())
    if "--test-telegram" in args:
        send("Test message from your ETH alert bot. If you can read this, Telegram is set up.")
        return
    if "--report" in args:
        send(scorecard(load_log()))
        return
    if "--backtest" in args:
        i = args.index("--backtest")
        try:
            days = float(args[i + 1])
        except Exception:
            days = 60.0
        run_backtest(min(max(days, 7.0), 365.0))
        return
    data = load_all(now)
    lg = load_log()
    resolve_open(lg, data, now)
    trade, in_trade = None, False
    st0 = load_state()
    try:
        trade = parse_trade(TRADE_TXT)
    except Exception as e:
        if "badparse" not in st0["sent"]:
            send(f"TRADE variable not understood ({e}). Format: side entry sl tp1 tp2 invalid, "
                 f"e.g. long 2470 2443 2511 2574 2455 (use 0 to skip tp2/invalid), or none.")
            st0["sent"].append("badparse")
            save_state(st0)
    if trade:
        in_trade = watch_trade(data, now, st0, trade)
        save_state(st0)
    res, notes = analyse(data)
    print(f"{datetime.now(ET):%H:%M ET}  price {data['15m'][-1]['c']:.2f}  "
          f"4h {trend(data['4h'])}  1h {trend(data['1h'])}")
    for n in notes:
        print("  note:", n)
    sent = False
    st = load_state()
    for side in ("long", "short"):
        full, near = res[side]
        if in_trade and PAUSE_IN_TRADE:
            print("  trade open: new setup alerts paused")
            break
        for kind, s in (("full", full), ("near", near if NEAR_MISS else None)):
            if not s:
                continue
            if kind == "near" and full:
                continue
            s["kind"] = kind
            age_min = (now - s["candle_close_t"]) / 60
            if "--force" not in args and age_min > RUN_WINDOW_MIN:
                print(f"  {kind} {side} setup is stale ({age_min:.0f} min old), skipping")
                continue
            key = f"{kind}:{side}:{s['candle_close_t']}"
            zone_key = f"{kind}:{side}:{round(s['level'])}"
            recent = [k for k in st["sent"] if k.startswith(zone_key + "@")
                      and now - int(k.split("@")[1]) < 4 * 3600]
            if "--force" not in args and (key in st["sent"] or recent):
                print(f"  {kind} {side} already alerted, skipping")
                continue
            send(build_message(side, s))
            log_alert(lg, side, s, kind, now)
            st["sent"] += [key, f"{zone_key}@{now}"]
            sent = True
    save_state(st)
    save_log(lg)
    utc = datetime.now(timezone.utc)
    if "--status" in args or (utc.hour == HEARTBEAT_HOUR_UTC and utc.minute < 15 and not sent):
        send(build_status(data))
    if utc.weekday() == 0 and utc.hour == HEARTBEAT_HOUR_UTC and utc.minute < 15 and lg["alerts"]:
        send(scorecard(lg))  # weekly scorecard on Mondays
    if not sent:
        print("no alert this run")


if __name__ == "__main__":
    main()
