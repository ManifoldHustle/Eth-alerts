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
RUN_WINDOW_MIN = int(_env("RUN_WINDOW_MIN", "35"))  # only alert on a candle that closed recently
STATE_FILE = _env("STATE_FILE", "state.json")      # remembers alerts already sent (no duplicates)
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
            tgt.add(round(h, 2))
        for _, h in pivots(c4h[-120:], 2)[0]:
            tgt.add(round(h, 2))
        tgt.add(round(max(c["h"] for c in c15[-96:]), 2))  # 24h high
        ups = sorted(x for x in tgt if x - buf > entry)
        tp1 = next((x - buf for x in ups if x - buf - entry >= 0.8 * risk), None)
        if tp1 is None:
            notes.append(f"{label} {lv:.2f}: no target far enough")
            continue
        tp2 = next((x - buf for x in ups if x - buf >= tp1 + 1.5 * a1), None)
        if tp2:
            rr = ((tp1 - entry) / 3 + 2 * (tp2 - entry) / 3) / risk
            w = (1 / 3, 2 / 3)
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
    p = lambda x: f"{x:,.2f}"
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
        lines.append(f"TP1: {p(s['tp1'])}  close {size / 3:.3f} ETH, then move SL to entry")
        lines.append(f"TP2: {p(s['tp2'])}  close {size * 2 / 3:.3f} ETH")
    else:
        lines.append(f"TP: {p(s['tp1'])}  close all {size:.3f} ETH")
    lines += [
        f"R:R ≈ {s['rr']:.2f}:1",
        "",
        f"Skip it if price has already moved more than {0.25 * s['atr1h']:.1f} points from the entry "
        f"({'above' if side == 'long' else 'below'}).",
        f"Cancel the idea if a 1h candle closes {'below' if side == 'long' else 'above'} {p(s['sl'])}.",
        "Time stop: close at market if TP1 isn't hit within 48h.",
        "Send me the 15m/1h/4h screenshots to double-check before you place it. Not financial advice.",
    ]
    return "\n".join(lines)


def build_status(data):
    c15, c1h, c4h = data["15m"], data["1h"], data["4h"]
    a1 = atr(c1h)
    r1 = rsi([c["c"] for c in c1h])
    return (f"Bot alive. ETH {c15[-1]['c']:,.2f} | 4h {trend(c4h)} | 1h {trend(c1h)} | "
            f"1h RSI {r1:.0f} | 1h ATR {a1:.1f}. Watching for setups.")


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


def analyse(data):
    c15, c1h, c4h = data["15m"], data["1h"], data["4h"]
    lf, ln, n1 = find_long(c15, c1h, c4h)
    sf, sn, n2 = find_long(mirror(c15), mirror(c1h), mirror(c4h))
    out = {"long": (lf, ln), "short": (to_short(sf), to_short(sn))}
    for side in ("long", "short"):
        full, near = out[side]
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
    data = load_all(now)
    res, notes = analyse(data)
    print(f"{datetime.now(ET):%H:%M ET}  price {data['15m'][-1]['c']:.2f}  "
          f"4h {trend(data['4h'])}  1h {trend(data['1h'])}")
    for n in notes:
        print("  note:", n)
    sent = False
    st = load_state()
    for side in ("long", "short"):
        full, near = res[side]
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
            st["sent"] += [key, f"{zone_key}@{now}"]
            sent = True
    save_state(st)
    utc = datetime.now(timezone.utc)
    if "--status" in args or (utc.hour == HEARTBEAT_HOUR_UTC and utc.minute < 15 and not sent):
        send(build_status(data))
    if not sent:
        print("no alert this run")


if __name__ == "__main__":
    main()
