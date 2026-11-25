"""SRS / ASRS drawn directly on ATM option charts (owner 2026-11-20 idea:
"buy side go with CE, sell side PE, so SL can be realistic").

Real 1-minute candles of the NIFTY November MONTHLY contract (expiry 2026-11-23),
which Kite still serves because it has not expired.  Rules fixed before looking:
  * strike  = nearest 50 to NIFTY's 09:29 minute close (known before the 09:30 bar);
    nearest 100 before 2026-10-28 (far-month contract lists 100-wide strikes)
  * zero-volume minutes are not trades (see bracket); index comparison row is INDICATIVE
    (index takes one side per day, 0.013% buffer, 15:10 exit)
  * per option (CE and PE separately, BUY side only):
      ASRS signal = 09:30-09:34 minutes (the 09:30 5-min bar)
      SRS  signal = 09:30-09:44 minutes (the 09:30-09:45 15-min bar)
      buy stop at signal high + buffer (0.13% of the high), stop = signal low - buffer
  * one trade per option per setup per day; last entry minute 15:04; exit = 15:09 minute close
  * same-minute entry+stop: pessimistic = stopped (unless the fill was the minute's open)
  * costs per lot (65): Rs20/order + GST, STT 0.15% of sell premium, NSE 0.03503%/side + GST,
    stamp 0.003% buy, slippage 0.75 premium pt per side (half of a ~1.5 pt spread)

    python -I options_check.py fetch       # cache 1-min data to the scratchpad
    python -I options_check.py run [from] [to]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

LOT = 65
BUF = 0.0013
SLIP = 0.75
CACHE = Path(r"C:/Users/mail2/AppData/Local/Temp/claude/cab3a769884d34b1/"
             r"b6d89720-ff27-454d-9c33-e29855d0a4dc/scratchpad/srs_week/nov_series")
INSTR = CACHE.parent / "nov_instruments.json"


def traded(b: dict) -> bool:
    """A minute with zero volume is Kite repeating the last close, not a trade.
    Bars without a volume field (the index, test bars) count as traded."""
    return b.get("v", 1) > 0


def bracket(mins: list[dict], t0: str, t1: str, pessimistic: bool = True,
            last_entry: str = "15:04", exit_t: str = "15:09",
            worst_case: bool = False, step: int = 1) -> dict | None:
    """Buy-stop bracket on one instrument's 1-min bars (t = 'HH:MM').

    Thin-data rules (review 11-20, far-month contract):
      * a signal window containing any zero-volume minute is not tradeable;
      * a zero-volume minute cannot trigger an entry or a stop;
      * the first traded minute after a zero-volume one has a STALE open/low/high
        seeded from the old close: an entry there fills at the bar's HIGH and a
        stop at the bar's LOW (worst case), never at the level.
      * worst_case=True (far-month days): Kite seeds EVERY bar's open from the
        previous close, which on thin bars is not a trade (re-review 11-20), so
        every entry fills at the bar's high and every stop at the bar's low.
      * a missing minute inside the signal window also makes it not tradeable.
    """
    sig = [b for b in mins if t0 <= b["t"] < t1]
    if not sig:
        return None
    hh0, mm0, hh1, mm1 = int(t0[:2]), int(t0[3:]), int(t1[:2]), int(t1[3:])
    n_expected = ((hh1 * 60 + mm1) - (hh0 * 60 + mm0)) // step   # step = bar length in minutes
    if len(sig) != n_expected or not all(traded(b) for b in sig):
        return dict(entry_t=None, thin_window=True)
    hi, lo = max(b["h"] for b in sig), min(b["l"] for b in sig)
    buf = BUF * hi
    level, stop = hi + buf, lo - buf
    pos = None
    prev_traded = True
    for b in mins:
        if b["t"] < t1:
            prev_traded = traded(b)
            continue
        if b["t"] > exit_t:
            break
        tr, stale = traded(b), (not prev_traded) or worst_case
        prev_traded = tr
        if not tr:
            if pos is not None and b["t"] == exit_t:
                pos.update(exit_t=b["t"], exit_px=b["c"], reason="eod", stale_exit=True)
                return pos
            continue
        if pos is None:
            if b["t"] > last_entry or b["h"] < level:
                continue
            if stale:
                fill, at_open = b["h"], False
            else:
                at_open = b["o"] >= level
                fill = b["o"] if at_open else level
            pos = dict(entry_t=b["t"], fill=fill, stop=stop, level=level, hi=hi, lo=lo,
                       stale_entry=stale)
            if b["l"] <= stop and (at_open or pessimistic or b["c"] <= stop):
                pos.update(exit_t=b["t"], exit_px=(b["l"] if stale else stop), reason="stop")
                return pos
        else:
            if stale and b["l"] <= stop:
                pos.update(exit_t=b["t"], exit_px=b["l"], reason="stop", stale_stop=True)
                return pos
            if not stale and b["o"] <= stop:
                pos.update(exit_t=b["t"], exit_px=b["o"], reason="stop")
                return pos
            if b["l"] <= stop:
                pos.update(exit_t=b["t"], exit_px=stop, reason="stop")
                return pos
        if pos is not None and b["t"] == exit_t:
            pos.update(exit_t=b["t"], exit_px=b["c"], reason="eod")
            return pos
    if pos is not None:                       # data ended early (e.g. a live day)
        last = [b for b in mins if b["t"] <= exit_t][-1]
        pos.update(exit_t=last["t"], exit_px=last["c"], reason="open")
    return pos if pos else dict(level=level, stop=stop, hi=hi, lo=lo, entry_t=None)


FRONT_FROM = "2026-10-28"     # Oct monthly expired 27-Oct: Nov is the front month from here
FAR_SLIP = 3.0                # owner 11-20: far-month (Sep/Oct) is illiquid -> bigger slippage


def liquidity(mins: list[dict], t0: str = "09:30", t1: str = "09:45") -> dict:
    """Missing minutes and zero-volume minutes in the signal window."""
    want = [f"09:{mm:02d}" for mm in range(int(t0[3:]), int(t1[3:]))]
    have = {b["t"]: b for b in mins}
    return dict(missing=sum(t not in have for t in want),
                zero_vol=sum(t in have and not have[t].get("v") for t in want),
                vol=sum(have[t].get("v", 0) for t in want if t in have))


def cost_rs(entry: float, exit_px: float, slip: float | None = None) -> float:
    brk = 2 * 20 * 1.18
    stt = 0.0015 * exit_px * LOT
    exch = 0.0003503 * (entry + exit_px) * LOT * 1.18
    stamp = 0.00003 * entry * LOT
    slip = 2 * (SLIP if slip is None else slip) * LOT
    return brk + stt + exch + stamp + slip


def strike_for(nifty_mins: list[dict], step: int = 50) -> int | None:
    """Nearest strike to NIFTY's 09:29 close.  step=100 on far-month days: the far
    contract lists 100-wide strikes; 50s are added near expiry."""
    pre = [b for b in nifty_mins if b["t"] <= "09:29"]
    return int(round(pre[-1]["c"] / step) * step) if pre else None


MIN_CANDLES = 300             # fewer 1-min candles on a day = not tradeable, excluded


# ---------------------------------------------------------------- fetch (read-only)
def fetch() -> None:
    from kiteconnect import KiteConnect
    root = Path(__file__).resolve().parent.parent
    tok = json.loads((root / "data/kite_access_token.json").read_text())
    k = KiteConnect(api_key=tok["api_key"])
    k.set_access_token(tok["access_token"])
    ins = json.loads(INSTR.read_text())
    toks = {(i["strike"], i["typ"]): i["tok"] for i in ins["items"]}
    nifty_tok = k.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["instrument_token"]
    CACHE.mkdir(parents=True, exist_ok=True)
    conv = lambda rows: [dict(t=c["date"].strftime("%H:%M"), o=c["open"], h=c["high"],
                              l=c["low"], c=c["close"], v=c.get("volume", 0)) for c in rows]
    d = date(2026, 9, 1)
    while d <= date.today():
        f = CACHE / f"{d}.json"
        if d.weekday() < 5 and not f.exists():
            nifty = conv(k.historical_data(nifty_tok, d, d, "minute"))
            time.sleep(0.35)
            if nifty:
                s = strike_for(nifty, 50 if str(d) >= FRONT_FROM else 100)
                rec = dict(day=str(d), strike=s, nifty=nifty)
                for typ in ("CE", "PE"):
                    tk = toks.get((float(s), typ))
                    rec[typ] = conv(k.historical_data(tk, d, d, "minute")) if tk else []
                    time.sleep(0.35)
                f.write_text(json.dumps(rec))
                print(d, s, len(nifty), len(rec["CE"]), len(rec["PE"]), flush=True)
        d += timedelta(days=1)


# ---------------------------------------------------------------- run
def to5(mins: list[dict]) -> list[dict]:
    """1-min -> 5-min bars aligned at 09:15 (for the reviewed index simulator)."""
    out: dict[str, dict] = {}
    for b in mins:
        hh, mm = int(b["t"][:2]), int(b["t"][3:])
        k5 = f"{hh:02d}:{mm - mm % 5:02d}"
        if k5 not in out:
            out[k5] = dict(t="2026-01-01 " + k5, o=b["o"], h=b["h"], l=b["l"], c=b["c"])
        else:
            x = out[k5]
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    return [out[k] for k in sorted(out)]


def run(d0: str = "2026-09-01", d1: str = "2099-12-31") -> list[dict]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sim
    rows = []
    for f in sorted(CACHE.glob("*.json")):
        rec = json.loads(f.read_text())
        if not (d0 <= rec["day"] <= d1):
            continue
        if len(rec["nifty"]) < 375:
            continue                          # partial (live) day: reported separately
        n5 = to5(rec["nifty"])
        thin = {typ: len(rec[typ]) < MIN_CANDLES for typ in ("CE", "PE")}
        idx_ok = sim.valid_day(n5) if len(n5) == sim.BARS_PER_DAY else False
        for setup, t1, sig_n in (("ASRS", "09:35", 1), ("SRS", "09:45", 3)):
            r = dict(day=rec["day"], setup=setup, strike=rec["strike"],
                     front=rec["day"] >= FRONT_FROM)
            for typ in ("CE", "PE"):
                t = None if thin[typ] else bracket(rec[typ], "09:30", t1,
                                                   worst_case=rec["day"] < FRONT_FROM)
                r[typ] = t if t else dict(entry_t=None, nodata=thin[typ])
                r[typ + "_liq"] = liquidity(rec[typ], "09:30", t1)
                if t and t.get("entry_t"):
                    pts = t["exit_px"] - t["fill"]
                    t["pts"] = pts
                    t["gross_rs"] = pts * LOT
                    t["net_rs"] = pts * LOT - cost_rs(t["fill"], t["exit_px"])
                    t["net_rs_far_slip"] = pts * LOT - cost_rs(t["fill"], t["exit_px"], FAR_SLIP)
            if idx_ok:
                ts = sim.all_paths(lambda ch: sim.breakout_day(n5, sim.Spec(3, sig_n), ch))
                worst = min(ts, key=lambda p: sum(x.pts for x in p))
                r["index"] = [dict(side=x.side, pts=x.pts, reason=x.reason,
                                   net_pts=x.pts - sim.cost_points(x.entry_px, rec["day"], 1.0))
                              for x in worst]
            rows.append(r)
    return rows


if __name__ == "__main__":
    if sys.argv[1:2] == ["fetch"]:
        fetch()
    else:
        a = sys.argv[2:]
        res = run(*a)
        print(json.dumps(res)[:200])
