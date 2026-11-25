"""Quick look (owner 2026-11-20): ASRS / SRS on the Chartink "stocks in play" 09:30 picks for 19-Nov and 20-Nov.

Uses the reviewed sim.breakout_day (bracket, pessimistic intrabar, first trigger only, opposite-level stop).
Stocks: NSE cash 5-min bars from Kite (read-only).  Hold to the 15:10 bar close; on a LIVE day the exit is the last
COMPLETED 5-min bar (mark-to-market, reason 'open').  Costs: cash intraday (MIS), Kotak NEO zero brokerage.

    python -I stocks_asrs_check.py fetch      # cache bars to the scratchpad
    python -I stocks_asrs_check.py run
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim  # noqa: E402

PICKS = {
    "2026-11-19": ["BANKBARODA", "BDL", "BLUESTARCO", "EICHERMOT", "ETERNAL", "HEROMOTOCO", "HINDALCO", "IREDA",
                   "MANAPPURAM", "MUTHOOTFIN", "PAYTM", "PGEL", "PNB", "POLICYBZR", "POLYCAB", "SBIN", "TITAN",
                   "VOLTAS"],
    "2026-11-20": ["ADANIPOWER", "APLAPOLLO", "APOLLOHOSP", "AXISBANK", "BAJAJ-AUTO", "BLUESTARCO", "COFORGE",
                   "CROMPTON", "GVT&D", "HINDZINC", "IREDA", "LTM", "MANAPPURAM", "MARUTI", "MAXHEALTH", "MPHASIS",
                   "MUTHOOTFIN", "NATIONALUM", "PAYTM", "PERSISTENT", "POLYCAB", "TATASTEEL", "TITAN", "TMPV",
                   "ULTRACEMCO", "VEDL", "VOLTAS"],
}
CACHE = Path(r"C:/Users/mail2/AppData/Local/Temp/claude/cab3a769884d34b1/"
             r"b6d89720-ff27-454d-9c33-e29855d0a4dc/scratchpad/srs_week/stocks5")
RISK_RS = 1000.0               # fixed rupee risk per trade for the Rs column

# cash intraday (MIS) charges, NEO brokerage 0
STT_SELL = 0.00025
NSE_TXN = 0.0000297            # per side
SEBI = 0.000001                # per side
STAMP_BUY = 0.00003
GST = 0.18
SLIP = 0.0002                  # per side, estimate for liquid F&O names


def cost_per_share(side: int, entry: float, exit_px: float) -> float:
    """Rupees per share for one round trip.  STT on the SELL leg: exit for a long, entry for a short.
    Stamp on the BUY leg: entry for a long, exit for a short."""
    sell_px = exit_px if side > 0 else entry
    buy_px = entry if side > 0 else exit_px
    txn = (NSE_TXN + SEBI) * (entry + exit_px)
    return STT_SELL * sell_px + STAMP_BUY * buy_px + txn * (1 + GST) + SLIP * (entry + exit_px)


def fetch() -> None:
    from kiteconnect import KiteConnect
    root = Path(__file__).resolve().parent.parent
    tok = json.loads((root / "data/kite_access_token.json").read_text())
    k = KiteConnect(api_key=tok["api_key"])
    k.set_access_token(tok["access_token"])
    inst = {i["tradingsymbol"]: i["instrument_token"] for i in k.instruments("NSE")}
    CACHE.mkdir(parents=True, exist_ok=True)
    for d, syms in PICKS.items():
        dd = date.fromisoformat(d)
        for s in syms:
            f = CACHE / f"{d}_{s.replace('&', '_')}.json"
            if f.exists() and d != str(date.today()):
                continue
            if s not in inst:
                print("MISSING symbol", s)
                continue
            rows = k.historical_data(inst[s], dd, dd, "5minute")
            bars = [dict(t=c["date"].strftime("%Y-%m-%d %H:%M"), o=c["open"], h=c["high"], l=c["low"],
                         c=c["close"]) for c in rows]
            f.write_text(json.dumps(bars))
            time.sleep(0.35)
    print("fetched", datetime.now().strftime("%H:%M"))


def completed(bars: list[dict], now_hhmm: str | None) -> list[dict]:
    """Keep only contiguous bars from 09:15; on a live day drop the bar still forming."""
    out = []
    for i, b in enumerate(bars):
        m = 9 * 60 + 15 + 5 * i
        if b["t"][11:] != f"{m // 60:02d}:{m % 60:02d}":
            break
        out.append(b)
    if now_hhmm is not None and out:
        hh, mm = int(now_hhmm[:2]), int(now_hhmm[3:])
        last_start = hh * 60 + mm - 5            # a bar is complete once 5 minutes have passed since its start
        out = [b for b in out if int(b["t"][11:13]) * 60 + int(b["t"][14:16]) <= last_start]
    return out


def trade(bars: list[dict], d: str, sig_n: int) -> dict | None:
    if len(bars) < 4 + sig_n:
        return None
    last = min(len(bars) - 1, sim.EXIT_IDX)
    spec = sim.Spec(3, sig_n, exit_idx=last, last_entry_idx=min(sim.LAST_ENTRY_IDX, last))
    sig = bars[3:3 + sig_n]
    rng = max(b["h"] for b in sig) - min(b["l"] for b in sig)
    if rng < 0.001 * sig[-1]["c"]:                     # frozen / circuit signal bar: R would be absurd
        return None
    paths = sim.all_paths(lambda ch: sim.breakout_day(bars, spec, ch))
    worst = min(paths, key=lambda p: sum(t.pts for t in p))
    if not worst:
        el, es, _, _ = sim.levels(bars, spec)
        return dict(trig=False, el=el, es=es)
    t = worst[0]
    reason = t.reason if (t.reason == "stop" or last == sim.EXIT_IDX) else "open"
    c = cost_per_share(t.side, t.entry_px, t.exit_px)
    return dict(trig=True, side=t.side, entry_t=bars[t.entry_i]["t"][11:], entry=t.entry_px, stop=t.stop,
                exit_t=bars[t.exit_i]["t"][11:], exit=t.exit_px, reason=reason, risk=t.risk,
                r_gross=t.pts / t.risk, r_net=(t.pts - c) / t.risk, cost_r=c / t.risk,
                amb=len(paths) > 1)


def run() -> None:
    now = datetime.now().strftime("%H:%M")
    today = str(date.today())
    for setup, sig_n in (("ASRS", 1), ("SRS", 3)):
        print(f"\n===== {setup} (risk Rs {RISK_RS:,.0f}/trade; NEO cash intraday costs) =====")
        for d, syms in PICKS.items():
            tot_r = tot_g = 0.0
            n = wins = 0
            print(f"\n-- {d}{' (LIVE: open trades marked at last completed bar)' if d == today else ''}")
            for s in syms:
                f = CACHE / f"{d}_{s.replace('&', '_')}.json"
                if not f.exists():
                    print(f"   {s:11s} no data")
                    continue
                bars = completed(json.loads(f.read_text()), now if d == today else None)
                x = trade(bars, d, sig_n)
                if x is None:
                    print(f"   {s:11s} too few bars")
                    continue
                if not x["trig"]:
                    print(f"   {s:11s} no trigger (buy>{x['el']:.2f} / sell<{x['es']:.2f})")
                    continue
                n += 1
                wins += x["r_net"] > 0
                tot_r += x["r_net"]
                tot_g += x["r_gross"]
                print(f"   {s:11s} {'LONG ' if x['side'] > 0 else 'SHORT'} {x['entry_t']} @ {x['entry']:.2f} "
                      f"stop {x['stop']:.2f} -> {x['reason']:4s} {x['exit_t']} @ {x['exit']:.2f} | "
                      f"{x['r_gross']:+.2f}R gross, cost {x['cost_r']:.2f}R, {x['r_net']:+.2f}R net = "
                      f"Rs {x['r_net'] * RISK_RS:+,.0f}" + ("  [same-bar: pessimistic]" if x["amb"] else ""))
            if n:
                print(f"   TOTAL {d}: {n} trades, {wins} net winners, gross {tot_g:+.2f}R, net {tot_r:+.2f}R "
                      f"= Rs {tot_r * RISK_RS:+,.0f}  (avg {tot_r / n:+.2f}R/trade)")


if __name__ == "__main__":
    fetch() if sys.argv[1:2] == ["fetch"] else run()
