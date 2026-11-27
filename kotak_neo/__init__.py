"""kotak_neo -- the ONE Kotak NEO package every bot uses (owner, 2026-11-27).

One account, one login place, one session. BTST, zebra, ETF_PI and any future
bot read the session this package publishes and send orders through it; none
of them logs in on its own. Design and the live measurements behind it:
`BOTS/neo_session/DESIGN.md` (untracked); build plan K0-K6.

Dependencies: the stdlib and `requests` (2.25+) only, so the Pi's venv
(3.11) and `/usr/bin/python3` import it identically. Code stays 3.8-valid.

Layout:
  envelope.py  how to read a NEO reply (message keys, dead session, scrubbing)
  session.py   the session file, generation, single-flight refresh, the brakes.
               The ONLY module allowed to call NEO's login endpoints.
  client.py    NeoClient(bot): reads + place/cancel on the shared session; bot
               tags, one bot per contract, never re-sends, never logs in on an order
  orders.py    resolve(tag), cancel_and_confirm, chase_exit (one step per call)
  symbols.py   Kite row -> NEO row for NIFTY and stock options; daily shared cache
  fake.py      FakeNeo, the measured reply shapes + scriptable faults, for tests
  __main__.py  `python -m kotak_neo login|status|refresh|unblock`
"""
