"""Intraday NSE scanner.

Every FETCH_INTERVAL_MINUTES during market hours it pulls the whole F&O universe
from NSE (quotes, OI spurts, option chains for the top candidates), appends the
snapshot to oi_data_json/<date>.json, scores every stock for long and short
setups and writes the ranking to scan_results/<date>.json. The cycle at
FINAL_PICK_TIME (09:54) freezes the "morning pick" and sends it to Telegram,
so the list is out before 10:00.
"""
import fcntl
import glob
import json
import os
import threading
import time
import traceback
from datetime import datetime

import pytz
import requests

import config
import performance
from nse_client import NSEClient, NSEError

IST = pytz.timezone("Asia/Kolkata")
OPTION_CHAIN_BUDGET_SEC = 90   # keep each cycle well inside the 3-minute window

os.makedirs(config.JSON_DIR, exist_ok=True)
os.makedirs(config.SCAN_DIR, exist_ok=True)

STATE = {
    "running": False,
    "last_run": None,
    "last_error": None,
    "last_duration_sec": None,
    "market_closed_date": None,
}
_cycle_lock = threading.Lock()


# ==================== HELPERS ====================
def now_ist():
    return datetime.now(IST)


def num(v, default=None):
    """NSE sends numbers as int, float or strings like '1,234.50' / '-'."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return default


def hhmm_to_min(s):
    h, m = s.replace(":", "")[:2], s.replace(":", "")[2:4]
    return int(h) * 60 + int(m)


def pct_rank(values):
    """Map {key: value} -> {key: percentile 0..1}. None values are skipped."""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][1]: 0.5}
    return {k: i / (n - 1) for i, (v, k) in enumerate(items)}


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def day_file(date_str):
    return os.path.join(config.JSON_DIR, f"{date_str}.json")


def scan_file(date_str):
    return os.path.join(config.SCAN_DIR, f"{date_str}.json")


# ==================== OPTION CHAIN ====================
def summarize_option_chain(raw, ltp=None, strikes_each_side=2):
    """ATM ± N strikes: total CE/PE OI and change in OI, plus whole-chain PCR."""
    records = raw.get("records", {}) or {}
    rows = records.get("data") or (raw.get("filtered", {}) or {}).get("data") or []
    expiries = records.get("expiryDates") or []
    if expiries and rows and "expiryDate" in rows[0]:
        rows = [r for r in rows if r.get("expiryDate") == expiries[0]]
    if not rows:
        return None

    underlying = num(records.get("underlyingValue")) or ltp
    by_strike = {}
    for r in rows:
        k = num(r.get("strikePrice"))
        if k is not None:
            by_strike[k] = r
    strikes = sorted(by_strike)
    if not strikes or not underlying:
        return None

    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying))
    near = strikes[max(0, atm_i - strikes_each_side): atm_i + strikes_each_side + 1]

    def side_sum(keys, side, field):
        return sum(num((by_strike[k].get(side) or {}).get(field), 0) for k in keys)

    ce_total = side_sum(strikes, "CE", "openInterest")
    pe_total = side_sum(strikes, "PE", "openInterest")
    return {
        "atm": strikes[atm_i],
        "ce_oi": side_sum(near, "CE", "openInterest"),
        "pe_oi": side_sum(near, "PE", "openInterest"),
        "ce_chg": side_sum(near, "CE", "changeinOpenInterest"),
        "pe_chg": side_sum(near, "PE", "changeinOpenInterest"),
        "pcr": round(pe_total / ce_total, 2) if ce_total else None,
    }


# ==================== SNAPSHOT ====================
def build_records(quotes, spurts, time_code):
    spurt_map = {s.get("symbol"): s for s in spurts or []}
    records = []
    for q in quotes:
        sym = q.get("symbol")
        ltp, open_, prev = num(q.get("lastPrice")), num(q.get("open")), num(q.get("previousClose"))
        if not sym or not ltp:
            continue
        rec = {
            "Stock": sym,
            "Time_Code": time_code,
            "Cash_Price": ltp,
            "Cash_Change_%_Open": round((ltp - open_) / open_ * 100, 2) if open_ else 0.0,
            "Change_%": num(q.get("pChange"), 0.0),
            "Open": open_,
            "High": num(q.get("dayHigh")),
            "Low": num(q.get("dayLow")),
            "Prev_Close": prev,
            "Volume": num(q.get("totalTradedVolume"), 0.0),
            "Turnover_Cr": round(num(q.get("totalTradedValue"), 0.0) / 1e7, 2),
            "Change_30d_%": num(q.get("perChange30d")),
        }
        s = spurt_map.get(sym)
        if s:
            rec["OI"] = num(s.get("latestOI"))
            rec["OI_Change_%"] = num(s.get("avgInOI"))
            if rec["OI_Change_%"] is None:
                prev_oi = num(s.get("prevOI"))
                if prev_oi:
                    rec["OI_Change_%"] = round((rec["OI"] - prev_oi) / prev_oi * 100, 2)
        records.append(rec)
    return records


def previous_day_context(date_str):
    """Prev day's high/low/close and volume-by-time from the last stored day file."""
    files = sorted(f for f in glob.glob(os.path.join(config.JSON_DIR, "*.json"))
                   if os.path.basename(f)[:10] < date_str)
    for path in reversed(files):
        data = _read_json(path, {})
        rows = data.get("combined_data", []) if isinstance(data, dict) else []
        if not rows:
            continue
        ctx = {}
        for r in rows:
            c = ctx.setdefault(r["Stock"], {"vol_by_time": {}})
            c["vol_by_time"][r["Time_Code"]] = r.get("Volume") or 0
            c["high"], c["low"], c["close"] = r.get("High"), r.get("Low"), r.get("Cash_Price")
        return ctx
    return {}


# ==================== SCORING ====================
WEIGHTS = {
    "momentum": 25,     # day change and change from open
    "strength": 15,     # where price sits in today's range
    "volume": 15,       # volume vs yesterday same time (or turnover rank)
    "oi": 15,           # OI build-up in the price direction
    "breakout": 15,     # opening-range and previous-day high/low break
    "acceleration": 10, # last ~9 minutes of movement
    "options": 5,       # near-ATM option writers' positioning
}


def score_universe(records, history, prev_ctx, preopen, time_code):
    """Return (longs, shorts) sorted best first."""
    t_now = hhmm_to_min(time_code)
    feats = {}
    for r in records:
        sym = r["Stock"]
        ltp, high, low, open_ = r["Cash_Price"], r.get("High"), r.get("Low"), r.get("Open")
        prev_close = r.get("Prev_Close")
        if ltp < config.MIN_PRICE or r.get("Turnover_Cr", 0) < config.MIN_TURNOVER_CR:
            continue
        if not (open_ and prev_close and high and low):
            continue
        gap = (open_ - prev_close) / prev_close * 100
        if abs(gap) > config.MAX_GAP_PCT:
            continue

        hist = history.get(sym, [])
        # ~3 cycles back (9 minutes)
        mom = None
        if len(hist) >= 4 and hist[-4]["Cash_Price"]:
            mom = (ltp - hist[-4]["Cash_Price"]) / hist[-4]["Cash_Price"] * 100
        # opening range = first 15 minutes (snapshots up to 09:30)
        or_rows = [h for h in hist if hhmm_to_min(h["Time_Code"]) <= hhmm_to_min("0930")]
        or_high = max((h["High"] for h in or_rows if h.get("High")), default=None)
        or_low = min((h["Low"] for h in or_rows if h.get("Low")), default=None)
        or_ready = t_now > hhmm_to_min("0930") and or_high is not None

        pc = prev_ctx.get(sym, {})
        relvol = None
        prev_vol = pc.get("vol_by_time", {}).get(time_code)
        if prev_vol:
            relvol = r.get("Volume", 0) / prev_vol

        feats[sym] = {
            "rec": r, "gap": gap, "mom": mom, "relvol": relvol,
            "clv": (ltp - low) / (high - low) if high > low else 0.5,
            "or_high": or_high if or_ready else None,
            "or_low": or_low if or_ready else None,
            "pdh": pc.get("high"), "pdl": pc.get("low"),
            "preopen_chg": num((preopen or {}).get(sym, {}).get("pChange")),
        }

    if not feats:
        return [], []

    chg = {s: f["rec"]["Change_%"] for s, f in feats.items()}
    chg_open = {s: f["rec"]["Cash_Change_%_Open"] for s, f in feats.items()}
    mom = {s: f["mom"] for s, f in feats.items()}
    oi_chg = {s: f["rec"].get("OI_Change_%") for s, f in feats.items()}
    has_relvol = sum(1 for f in feats.values() if f["relvol"] is not None) > len(feats) / 2
    vol_metric = {s: (f["relvol"] if has_relvol else f["rec"].get("Turnover_Cr")) for s, f in feats.items()}

    r_chg, r_open, r_mom, r_oi, r_vol = (pct_rank(x) for x in (chg, chg_open, mom, oi_chg, vol_metric))

    longs, shorts = [], []
    for sym, f in feats.items():
        r = f["rec"]
        ltp = r["Cash_Price"]
        for side in ("LONG", "SHORT"):
            up = side == "LONG"
            # Direction must agree with both the day move and the move from open
            if up and not (r["Change_%"] > 0 and ltp > r["Open"]):
                continue
            if not up and not (r["Change_%"] < 0 and ltp < r["Open"]):
                continue

            def d(rank, key=sym):  # flip the percentile for shorts
                v = rank.get(key)
                return None if v is None else (v if up else 1 - v)

            reasons = []
            comp = {}
            comp["momentum"] = 0.5 * d(r_chg) + 0.5 * d(r_open)
            reasons.append(f"{r['Change_%']:+.2f}% day, {r['Cash_Change_%_Open']:+.2f}% from open")

            comp["strength"] = f["clv"] if up else 1 - f["clv"]
            if comp["strength"] >= 0.8:
                reasons.append("trading near day " + ("high" if up else "low"))

            v = r_vol.get(sym)
            comp["volume"] = v if v is not None else 0.5
            if f["relvol"] is not None:
                reasons.append(f"volume {f['relvol']:.1f}x yesterday")

            oc = r.get("OI_Change_%")
            if oc is None:
                comp["oi"] = 0.5
            elif oc > 0:   # fresh positions in the price direction = long/short build-up
                comp["oi"] = 0.5 + 0.5 * (r_oi.get(sym) or 0.5)
                reasons.append(("long" if up else "short") + f" build-up (OI {oc:+.1f}%)")
            else:          # short covering / long unwinding: weaker but supportive
                comp["oi"] = 0.4
                reasons.append(("short covering" if up else "long unwinding") + f" (OI {oc:+.1f}%)")

            b = 0.0
            if up:
                if f["or_high"] and ltp > f["or_high"]:
                    b += 0.5; reasons.append("above opening-range high")
                if f["pdh"] and ltp > f["pdh"]:
                    b += 0.5; reasons.append("above previous-day high")
            else:
                if f["or_low"] and ltp < f["or_low"]:
                    b += 0.5; reasons.append("below opening-range low")
                if f["pdl"] and ltp < f["pdl"]:
                    b += 0.5; reasons.append("below previous-day low")
            comp["breakout"] = b

            a = d(r_mom)
            comp["acceleration"] = a if a is not None else 0.5

            ce_chg, pe_chg = r.get("CE_OI_Chg"), r.get("PE_OI_Chg")
            if ce_chg is None or pe_chg is None:
                comp["options"] = 0.5
            else:
                support = (pe_chg - ce_chg) if up else (ce_chg - pe_chg)
                comp["options"] = 1.0 if support > 0 else 0.0
                if support > 0:
                    reasons.append(("put" if up else "call") + " writing near ATM")

            score = sum(WEIGHTS[k] * comp[k] for k in WEIGHTS)
            pick = {
                "symbol": sym, "side": side, "score": round(score, 1),
                "price": ltp, "change_pct": round(r["Change_%"], 2), "change_from_open_pct": r["Cash_Change_%_Open"],
                "gap_pct": round(f["gap"], 2), "turnover_cr": r.get("Turnover_Cr"),
                "oi_change_pct": oc, "pcr": r.get("PCR"),
                "components": {k: round(v, 2) for k, v in comp.items()},
                "reasons": reasons,
            }
            (longs if up else shorts).append(pick)

    longs.sort(key=lambda p: p["score"], reverse=True)
    shorts.sort(key=lambda p: p["score"], reverse=True)
    return longs, shorts


# ==================== TELEGRAM ====================
def send_telegram(text):
    if not (config.ENABLE_TELEGRAM and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        return r.ok
    except requests.RequestException as e:
        print(f"Telegram error: {e}")
        return False


def format_pick_message(result):
    lines = [f"<b>🌅 Morning Pick — {result['date']} {result['time_display']} IST</b>",
             f"Scanned {result['universe_size']} stocks, {result['eligible']} passed filters", ""]
    for title, picks in (("🟢 LONG", result["longs"]), ("🔴 SHORT", result["shorts"])):
        lines.append(f"<b>{title}</b>")
        if not picks:
            lines.append("  none")
        for i, p in enumerate(picks[:5], 1):
            lines.append(f"{i}. <b>{p['symbol']}</b> ₹{p['price']:,.2f} ({p['change_pct']:+.2f}%) "
                         f"score {p['score']}")
            lines.append("   " + "; ".join(p["reasons"][:3]))
        lines.append("")
    return "\n".join(lines)


# ==================== CYCLE ====================
class Scanner:
    def __init__(self, client=None):
        self.nse = client or NSEClient()

    def fetch_preopen(self, now=None):
        now = now or now_ist()
        date_str = now.strftime("%Y-%m-%d")
        try:
            pre = {m["symbol"]: {"iep": num(m.get("iep") or m.get("lastPrice")),
                                 "pChange": num(m.get("pChange")),
                                 "prev_close": num(m.get("previousClose"))}
                   for m in self.nse.preopen("FO")}
        except NSEError as e:
            STATE["last_error"] = f"preopen: {e}"
            return 0
        day = _read_json(day_file(date_str), {})
        if not isinstance(day, dict):
            day = {}
        day.setdefault("date", date_str)
        day.setdefault("combined_data", [])
        day["preopen"] = pre
        _write_json(day_file(date_str), day)
        return len(pre)

    def run_cycle(self, now=None):
        if not _cycle_lock.acquire(blocking=False):
            return None
        started = time.time()
        STATE["running"] = True
        try:
            now = now or now_ist()
            date_str = now.strftime("%Y-%m-%d")
            # Snap to the 3-minute slot so all records of a cycle share one Time_Code
            slot_min = now.minute - now.minute % config.FETCH_INTERVAL_MINUTES
            time_code = f"{now.hour:02d}{slot_min:02d}"

            quotes = self.nse.index_constituents(config.SCAN_INDEX)
            try:
                spurts = self.nse.oi_spurts()
            except NSEError as e:
                print(f"OI spurts unavailable: {e}")
                spurts = []
            records = build_records(quotes, spurts, time_code)
            if not records:
                raise NSEError("NSE returned no quotes")

            day = _read_json(day_file(date_str), {})
            if not isinstance(day, dict):
                day = {}
            day.setdefault("date", date_str)
            rows = [r for r in day.get("combined_data", []) if r.get("Time_Code") != time_code]
            history = {}
            for r in rows:
                history.setdefault(r["Stock"], []).append(r)
            for r in records:
                history.setdefault(r["Stock"], []).append(r)
            for h in history.values():
                h.sort(key=lambda x: x["Time_Code"])

            prev_ctx = previous_day_context(date_str)
            preopen = day.get("preopen", {})

            # First pass without options to choose whose option chain to pull
            longs, shorts = score_universe(records, history, prev_ctx, preopen, time_code)
            half = config.OPTION_CHAIN_LIMIT // 2
            candidates = [p["symbol"] for p in longs[:half]] + [p["symbol"] for p in shorts[:half]]
            rec_by_sym = {r["Stock"]: r for r in records}
            for sym in dict.fromkeys(candidates):
                if time.time() - started > OPTION_CHAIN_BUDGET_SEC:
                    break
                try:
                    oc = summarize_option_chain(self.nse.option_chain(sym), rec_by_sym[sym]["Cash_Price"])
                except NSEError as e:
                    print(f"Option chain {sym}: {e}")
                    continue
                if oc:
                    rec_by_sym[sym].update({"CE_OI": oc["ce_oi"], "PE_OI": oc["pe_oi"],
                                            "CE_OI_Chg": oc["ce_chg"], "PE_OI_Chg": oc["pe_chg"],
                                            "PCR": oc["pcr"], "ATM": oc["atm"]})

            longs, shorts = score_universe(records, history, prev_ctx, preopen, time_code)

            day["combined_data"] = rows + records
            _write_json(day_file(date_str), day)

            eligible = len({p["symbol"] for p in longs + shorts})
            result = {
                "date": date_str,
                "time": time_code,
                "time_display": f"{time_code[:2]}:{time_code[2:]}",
                "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "universe_size": len(records),
                "eligible": eligible,
                "longs": longs[:config.TOP_N],
                "shorts": shorts[:config.TOP_N],
            }
            scans = _read_json(scan_file(date_str), {})
            if not isinstance(scans, dict):
                scans = {}
            scans["latest"] = result
            in_session = config.FINAL_PICK_TIME.replace(":", "") <= time_code <= config.MARKET_CLOSE_TIME.replace(":", "")
            if in_session and not scans.get("morning_pick"):
                result["late"] = time_code >= "1000"
                scans["morning_pick"] = result
                scans["morning_pick_sent"] = send_telegram(format_pick_message(result))
            _write_json(scan_file(date_str), scans)

            STATE["last_run"] = result["generated_at"]
            STATE["last_error"] = None
            return result
        except Exception as e:
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            return None
        finally:
            STATE["running"] = False
            STATE["last_duration_sec"] = round(time.time() - started, 1)
            _cycle_lock.release()

    # ---------- scheduler ----------
    def market_open_today(self, date_str):
        """Holiday check via NSE market status; assume open if NSE can't be reached."""
        try:
            for m in self.nse.market_status().get("marketState", []):
                if m.get("market") == "Capital Market":
                    return str(m.get("marketStatus", "")).lower() != "closed"
        except NSEError:
            pass
        return True

    def run_forever(self):
        last_slot = None
        preopen_done = None
        status_checked = None
        report_done = None
        open_m, close_m = hhmm_to_min(config.MARKET_OPEN_TIME), hhmm_to_min(config.MARKET_CLOSE_TIME)
        pre_m = hhmm_to_min(config.PREOPEN_FETCH_TIME)
        print("📡 NSE scanner scheduler started")
        while True:
            try:
                now = now_ist()
                date_str = now.strftime("%Y-%m-%d")
                m = now.hour * 60 + now.minute
                if now.weekday() < 5 and STATE["market_closed_date"] != date_str:
                    if pre_m <= m < open_m and preopen_done != date_str:
                        preopen_done = date_str
                        n = self.fetch_preopen(now)
                        print(f"🌅 Pre-open snapshot: {n} stocks")
                    if open_m <= m <= close_m and now.minute % config.FETCH_INTERVAL_MINUTES == 0 \
                            and now.second >= 5:
                        slot = f"{date_str} {now:%H%M}"
                        if slot != last_slot:
                            last_slot = slot
                            if status_checked != date_str:
                                status_checked = date_str
                                if not self.market_open_today(date_str):
                                    STATE["market_closed_date"] = date_str
                                    print(f"🏖️ NSE closed today ({date_str})")
                                    continue
                            res = self.run_cycle(now)
                            if res:
                                print(f"✅ {res['time_display']} scanned {res['universe_size']} | "
                                      f"top long {res['longs'][0]['symbol'] if res['longs'] else '-'} | "
                                      f"top short {res['shorts'][0]['symbol'] if res['shorts'] else '-'}")
                    # After the close: score today's morning pick and send the result
                    if m >= close_m + 3 and report_done != date_str:
                        report_done = date_str
                        day = performance.save_day(date_str)
                        if day and day["summary"].get("trades"):
                            send_telegram(performance.format_day_message(day))
                            print(f"📊 Morning pick result: {day['summary']}")
            except Exception:
                traceback.print_exc()
            time.sleep(5)


_lock_handle = None


def start_background_scanner():
    """Start the scheduler once per machine (gunicorn may fork several workers)."""
    global _lock_handle
    if not config.ENABLE_SCANNER or _lock_handle is not None:
        return False
    handle = open(os.path.join(config.SCAN_DIR, ".scheduler.lock"), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    threading.Thread(target=Scanner().run_forever, daemon=True, name="nse-scanner").start()
    return True


if __name__ == "__main__":
    # One-off manual run: python scanner.py
    result = Scanner().run_cycle()
    print(json.dumps(result, indent=2) if result else f"Failed: {STATE['last_error']}")
