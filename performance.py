"""Forward test of the morning picks.

Uses only data the scanner already stores:
  scan_results/<date>.json  -> morning_pick (entry list, frozen at 09:54)
  oi_data_json/<date>.json  -> 3-minute price snapshots for the rest of the day

Each pick is entered at its 09:54 price and exited at the first of:
stop-loss, target, or EXIT_TIME (checked on the 3-minute prices).

Run:  python performance.py            -> report for every stored day
      python performance.py 2026-10-01 -> one day
"""
import glob
import json
import os
import sys

import config

STOP_LOSS_PCT = 1.0     # exit if the trade goes 1% against
TARGET_PCT = 2.0        # exit if the trade gains 2%
EXIT_TIME = "1515"      # otherwise square off here
RANK_BUCKETS = (1, 3, 5, 10)


def _read(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def simulate_trade(pick, prices):
    """prices: [(time_code, price)] after entry, sorted. Returns trade dict."""
    entry = pick["price"]
    sign = 1 if pick["side"] == "LONG" else -1
    best = worst = 0.0
    exit_price, exit_time, reason = None, None, "no data"
    for t, p in prices:
        if t > EXIT_TIME:
            break
        ret = sign * (p - entry) / entry * 100
        best, worst = max(best, ret), min(worst, ret)
        exit_price, exit_time, reason = p, t, "time"
        if ret <= -STOP_LOSS_PCT:
            reason = "stop-loss"
            break
        if ret >= TARGET_PCT:
            reason = "target"
            break
    ret = sign * (exit_price - entry) / entry * 100 if exit_price else None
    return {
        "symbol": pick["symbol"], "side": pick["side"], "score": pick["score"],
        "entry": entry, "exit": exit_price,
        "exit_time": f"{exit_time[:2]}:{exit_time[2:]}" if exit_time else None,
        "exit_reason": reason,
        "return_pct": round(ret, 2) if ret is not None else None,
        "max_gain_pct": round(best, 2), "max_loss_pct": round(worst, 2),
        "components": pick.get("components", {}),
    }


def evaluate_day(date_str):
    scans = _read(os.path.join(config.SCAN_DIR, f"{date_str}.json"))
    pick = scans.get("morning_pick")
    if not pick:
        return None
    rows = _read(os.path.join(config.JSON_DIR, f"{date_str}.json")).get("combined_data", [])
    series = {}
    for r in rows:
        if r.get("Time_Code", "") > pick["time"] and r.get("Cash_Price"):
            series.setdefault(r["Stock"], []).append((r["Time_Code"], r["Cash_Price"]))
    trades = []
    for side in ("longs", "shorts"):
        for rank, p in enumerate(pick.get(side, []), 1):
            t = simulate_trade(p, sorted(series.get(p["symbol"], [])))
            t["rank"] = rank
            trades.append(t)
    done = [t for t in trades if t["return_pct"] is not None]
    last_time = max((r["Time_Code"] for r in rows), default="")
    return {
        "date": date_str,
        "complete": last_time >= EXIT_TIME,
        "trades": trades,
        "summary": stats(done),
    }


def stats(trades):
    if not trades:
        return {"trades": 0}
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    return {
        "trades": len(rets),
        "win_rate": round(len(wins) / len(rets) * 100, 1),
        "avg_return": round(sum(rets) / len(rets), 2),
        "total_return": round(sum(rets), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "best": max(rets),
        "worst": min(rets),
    }


def signal_attribution(trades):
    """For each score component: avg return when it was strong (>=0.7) vs weak (<0.4)."""
    out = {}
    keys = sorted({k for t in trades for k in t["components"]})
    for k in keys:
        hi = [t["return_pct"] for t in trades if t["components"].get(k, 0) >= 0.7]
        lo = [t["return_pct"] for t in trades if t["components"].get(k, 1) < 0.4]
        out[k] = {
            "strong_n": len(hi), "strong_avg": round(sum(hi) / len(hi), 2) if hi else None,
            "weak_n": len(lo), "weak_avg": round(sum(lo) / len(lo), 2) if lo else None,
        }
    return out


def full_report(dates=None):
    if dates is None:
        dates = sorted(os.path.basename(f)[:10] for f in glob.glob(os.path.join(config.SCAN_DIR, "*.json")))
    days = [d for d in (evaluate_day(x) for x in dates) if d]
    trades = [t for d in days for t in d["trades"] if t["return_pct"] is not None]
    return {
        "settings": {"stop_loss_pct": STOP_LOSS_PCT, "target_pct": TARGET_PCT,
                     "exit_time": f"{EXIT_TIME[:2]}:{EXIT_TIME[2:]}", "entry": "09:54 morning pick price"},
        "days": len(days),
        "overall": stats(trades),
        "by_side": {s: stats([t for t in trades if t["side"] == s]) for s in ("LONG", "SHORT")},
        "by_rank": {f"top_{n}": stats([t for t in trades if t["rank"] <= n]) for n in RANK_BUCKETS},
        "exit_reasons": {r: sum(1 for t in trades if t["exit_reason"] == r)
                         for r in ("target", "stop-loss", "time")},
        "signals": signal_attribution(trades),
        "daily": [{"date": d["date"], "complete": d["complete"], **d["summary"]} for d in days],
    }


def save_day(date_str):
    """Store the day's result inside scan_results/<date>.json; returns it."""
    day = evaluate_day(date_str)
    if not day:
        return None
    path = os.path.join(config.SCAN_DIR, f"{date_str}.json")
    scans = _read(path)
    scans["performance"] = day
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(scans, f, separators=(",", ":"))
    os.replace(tmp, path)
    return day


def format_day_message(day):
    s = day["summary"]
    lines = [f"<b>📊 Morning Pick Result — {day['date']}</b>"]
    if not s.get("trades"):
        return lines[0] + "\nNo price data after entry."
    lines.append(f"Win rate {s['win_rate']}% · avg {s['avg_return']:+.2f}% · total {s['total_return']:+.2f}%")
    lines.append(f"(SL {STOP_LOSS_PCT}% · target {TARGET_PCT}% · exit {EXIT_TIME[:2]}:{EXIT_TIME[2:]})")
    lines.append("")
    for t in day["trades"]:
        if t["rank"] <= 5 and t["return_pct"] is not None:
            icon = "✅" if t["return_pct"] > 0 else "❌"
            lines.append(f"{icon} {t['side'][0]} {t['symbol']} {t['return_pct']:+.2f}% ({t['exit_reason']} {t['exit_time']})")
    return "\n".join(lines)


if __name__ == "__main__":
    report = full_report(sys.argv[1:] or None)
    o = report["overall"]
    print(f"\nForward test: {report['days']} day(s), settings {report['settings']}")
    if not o.get("trades"):
        print("No completed morning picks yet - let the scanner run for a few days.")
        sys.exit(0)
    print(f"\nOVERALL  trades {o['trades']} | win {o['win_rate']}% | avg {o['avg_return']:+.2f}% | "
          f"PF {o['profit_factor']} | best {o['best']:+.2f}% | worst {o['worst']:+.2f}%")
    for name, group in list(report["by_side"].items()) + list(report["by_rank"].items()):
        if group.get("trades"):
            print(f"{name:<8} trades {group['trades']:>4} | win {group['win_rate']:>5}% | avg {group['avg_return']:+.2f}%")
    print(f"\nExits: {report['exit_reasons']}")
    print("\nSignal check (avg return when signal strong vs weak):")
    for k, v in report["signals"].items():
        print(f"  {k:<13} strong {v['strong_avg']} (n={v['strong_n']})  weak {v['weak_avg']} (n={v['weak_n']})")
    print("\nDaily:")
    for d in report["daily"]:
        if d.get("trades"):
            print(f"  {d['date']}  win {d['win_rate']:>5}%  avg {d['avg_return']:+.2f}%  total {d['total_return']:+.2f}%"
                  + ("" if d["complete"] else "  (day incomplete)"))
