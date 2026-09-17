"""The index signal, and the buy level it has to be translated into.

Why this file exists (2026-09-17): the Core basket's entry is a NIFTY monthly
ST touch, but ST Watch only ever computed the line from NIFTYBEES's OWN prints.
Measured that day: NIFTY's monthly ST was 22,268 while NIFTYBEES's was 237.19,
which is NIFTY ~20,800 — the ETF prints freak highs and lows (215 days >3% off
the scaled index) that inflate its monthly ATR and drag its band ~7% down. So
the alert for the rarest signal in the playbook would have fired ~7% late, or
not at all. The index is now watched directly, and because an index cannot be
bought, the alert carries the level priced in the ETF that can be.
"""
import json

import pytest

from playbook.st_watch import config as cfg
from playbook.st_watch import watcher


# ── The watchlist covers the playbook ────────────────────────────────────

def test_every_playbook_asset_is_watched():
    """The three ETFs that had no alert at all are in, and so is the index."""
    symbols = {s['symbol'] for s in cfg.get_all_symbols(cfg.load_config())}
    # Added 2026-09-17: in the playbook's Tactical universe, absent from config.
    # (MON100 was already watched — an earlier claim that it was missing was
    # wrong, and "fixing" it added a duplicate JSON key that json.loads ate.)
    assert {'MOM30IETF', 'EBBETF0431'} <= symbols
    assert 'MON100' in symbols
    assert 'NIFTY 50' in symbols
    # Core + index + 14 tactical ETFs + 4 REITs
    assert len(symbols) == 22


def test_the_index_is_a_core_basket_with_a_label():
    """An unlabelled basket would print its raw key in alerts and status."""
    by_symbol = {s['symbol']: s for s in cfg.get_all_symbols(cfg.load_config())}
    basket = by_symbol['NIFTY 50']['basket']
    assert basket in watcher.BASKET_LABELS
    # `_format_alert` keys the CORE guidance off this prefix.
    assert basket.startswith('core')


def test_defaults_layer_holds_the_symbols_not_the_overlay():
    """Symbols belong in the TRACKED layer.

    The overlay wins on merge and is untracked, so a symbol added there would
    be invisible to git and would not reach the Pi over a pull.
    """
    defaults = json.loads(
        (cfg.PROJECT_ROOT / 'config' / 'st_watch_config.defaults.json')
        .read_text(encoding='utf-8'))
    assert 'NIFTY 50' in defaults['symbols']['core_index']
    overlay_path = cfg.PROJECT_ROOT / 'config' / 'st_watch_config.json'
    if overlay_path.exists():
        overlay = json.loads(overlay_path.read_text(encoding='utf-8'))
        assert 'symbols' not in overlay, (
            "the overlay shadows the tracked symbol list — an edit to "
            "st_watch_config.defaults.json would silently do nothing")


def test_the_symbol_file_has_no_duplicate_keys():
    """A repeated key is kept silently — `json.loads` returns only the last.

    2026-09-17: a second "MON100" was added to `tactical_etfs` on the wrong
    belief that it was missing. Nothing caught it — `get_all_symbols` dedups,
    so even the count of 22 above stayed correct, and the scan behaved. Only a
    parse that inspects the raw pairs can see it, which is why this test reads
    the file rather than the loaded config.
    """
    dupes = []

    def hook(pairs):
        keys = [k for k, _ in pairs]
        dupes.extend(sorted({k for k in keys if keys.count(k) > 1}))
        return dict(pairs)

    json.loads(
        (cfg.PROJECT_ROOT / 'config' / 'st_watch_config.defaults.json')
        .read_text(encoding='utf-8'), object_pairs_hook=hook)
    assert dupes == [], f"duplicate keys in the tracked symbol list: {dupes}"


# ── Index → tradeable level ──────────────────────────────────────────────

def test_index_level_is_converted_at_the_live_ratio():
    """22,268 on the index is ~254 in the ETF, at that day's ratio."""
    ltps = {'NIFTY 50': 23253.35, 'NIFTYBEES': 265.13}
    proxy = watcher._index_proxy_level('NIFTY 50', 23253.35, 22268.4, ltps)
    assert proxy is not None
    sym, level = proxy
    assert sym == 'NIFTYBEES'
    assert level == pytest.approx(253.9, abs=0.5)


def test_conversion_tracks_the_ratio_rather_than_assuming_one():
    """A stored ratio would decay as the ETF reinvests dividends.

    Same level, an ETF that has drifted 10% richer against the index: the buy
    price must move with it, not stay at yesterday's number.
    """
    base = watcher._index_proxy_level(
        'NIFTY 50', 23000.0, 22268.4, {'NIFTY 50': 23000.0, 'NIFTYBEES': 260.0})
    drifted = watcher._index_proxy_level(
        'NIFTY 50', 23000.0, 22268.4, {'NIFTY 50': 23000.0, 'NIFTYBEES': 286.0})
    assert drifted[1] == pytest.approx(base[1] * 1.1, rel=1e-6)


def test_a_missing_proxy_quote_produces_no_level():
    """Never fabricate a buy price from a half-filled LTP response."""
    assert watcher._index_proxy_level(
        'NIFTY 50', 23253.35, 22268.4, {'NIFTY 50': 23253.35}) is None
    assert watcher._index_proxy_level(
        'NIFTY 50', 0, 22268.4, {'NIFTYBEES': 265.13}) is None


def test_an_ordinary_etf_is_not_converted():
    """Only indices carry a proxy; NIFTYBEES's own alert stays as it was."""
    assert watcher._index_proxy_level(
        'NIFTYBEES', 265.13, 237.19, {'NIFTYBEES': 265.13}) is None


# ── What the alert says ──────────────────────────────────────────────────

def test_index_alert_carries_the_etf_buy_level():
    msg = watcher._format_alert(
        'NIFTY 50', 'core_index', 'monthly', 22300.0, 22268.4, 0.14, 'UP',
        proxy=('NIFTYBEES', 253.94))
    assert 'Buy NIFTYBEES at Rs 253.94' in msg
    assert 'CORE' in msg          # core guidance still fires for core_index


def test_alert_without_a_proxy_is_unchanged():
    msg = watcher._format_alert(
        'NIFTYBEES', 'core', 'monthly', 240.0, 237.19, 1.18, 'UP')
    assert 'Buy ' not in msg
    assert 'Support: Rs 237.19' in msg


def test_a_down_line_is_never_dressed_as_a_buy():
    """ST DOWN is resistance. The index's weekly line was DOWN on 09-17."""
    msg = watcher._format_alert(
        'NIFTY 50', 'core_index', 'weekly', 24800.0, 24869.0, -0.28, 'DOWN',
        proxy=('NIFTYBEES', 283.0))
    assert 'NOT a buy signal' in msg
    assert 'Resistance' in msg
    # The assertion this test was missing on 2026-09-17: it proved the
    # disclaimer was present while the contradicting instruction sat above it.
    assert 'Buy ' not in msg


def test_a_down_alert_outside_the_disclaimer_band_still_says_no_buy():
    """The disclaimer only renders within 1%; the threshold is 1.5%.

    So the dangerous message is the 1.1-1.5% DOWN one — no guidance text at
    all. It must not carry a buy level either.
    """
    msg = watcher._format_alert(
        'NIFTY 50', 'core_index', 'weekly', 24500.0, 24869.0, -1.4, 'DOWN',
        proxy=('NIFTYBEES', 280.0))
    assert 'Buy ' not in msg
    assert 'NOT a buy signal' not in msg      # confirms the band, not the fix


# ── The proxy must be quoted even in a filtered run ──────────────────────

def test_a_filtered_scan_still_fetches_the_proxy_quote():
    """`--symbol "NIFTY 50"` must still price NIFTYBEES, or the alert loses
    the only number the owner acts on."""
    symbols = [{'symbol': 'NIFTY 50', 'basket': 'core_index'}]
    all_st = {'NIFTY 50_monthly': {}, 'NIFTY 50_weekly': {}}
    assert set(watcher._ltp_symbols(symbols, all_st)) == {'NIFTY 50', 'NIFTYBEES'}


def test_symbols_without_st_data_are_not_quoted():
    """Unchanged behaviour: no ST computed means no LTP call for that symbol."""
    symbols = [{'symbol': 'NIFTYBEES', 'basket': 'core'},
               {'symbol': 'GOLDBEES', 'basket': 'core'}]
    all_st = {'NIFTYBEES_monthly': {}}
    assert watcher._ltp_symbols(symbols, all_st) == ['NIFTYBEES']
