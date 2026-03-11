"""CLI entry point: python -m stock_analyzer RELIANCE [--json] [--holding] [--compare] [--refresh]"""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional

# Add Helper to path for sibling imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stock_analyzer.config import load_config
from stock_analyzer.sources.screener import fetch_screener_data
from stock_analyzer.sources.kite_technical import fetch_technical_data
from stock_analyzer.sources.trendlyne import fetch_trendlyne_data
from stock_analyzer.engine.fundamental_scores import (
    compute_fundamental_score, piotroski_f_score, peg_ratio, dupont_analysis, altman_z_score
)
from stock_analyzer.engine.technical_scores import compute_technical_score
from stock_analyzer.sources.data_bridge import flatten_technical_data
from stock_analyzer.engine.composite_scorer import (
    compute_verdict, compute_momentum_score, compute_risk_score, compute_valuation_score
)
from stock_analyzer.formatters.json_export import format_json, format_comparison_json
from stock_analyzer.formatters.terminal import format_terminal, format_comparison_terminal


def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S"
    )


def _dataclass_to_dict(obj):
    """Convert dataclass to dict, handling nested dataclasses."""
    if obj is None:
        return None
    if hasattr(obj, 'to_dict'):
        return obj.to_dict()
    if hasattr(obj, '__dataclass_fields__'):
        import dataclasses
        result = {}
        for f in dataclasses.fields(obj):
            val = getattr(obj, f.name)
            if hasattr(val, '__dataclass_fields__'):
                result[f.name] = _dataclass_to_dict(val)
            elif isinstance(val, list):
                result[f.name] = [_dataclass_to_dict(v) if hasattr(v, '__dataclass_fields__') else v for v in val]
            else:
                result[f.name] = val
        return result
    return obj


def _normalize_screener_for_engine(raw):
    """Convert screener parallel-array format to row-of-dicts for engine.

    Screener returns: {"annual": {"years": [...], "sales": [...], "net_profit": [...]}}
    Engine expects:   {"annual_results": [{"sales": x, "net_profit": y}, ...]}
                      with index 0 = most recent year.
    Also maps all_ratios -> ratios for consistency.
    """
    if not raw:
        return raw

    d = dict(raw)  # shallow copy

    def _parse_num(val):
        """Parse screener number strings like '24.5', '18,80,950Cr.', '9.69%' to float."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        if not s or s == '-':
            return None
        # Remove common suffixes
        s = s.replace('Cr.', '').replace('Cr', '').replace('%', '').replace(',', '').strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    # Map all_ratios -> ratios with numeric conversion
    raw_ratios = d.get('all_ratios', d.get('ratios', {}))
    if raw_ratios:
        d['ratios'] = {}
        for k, v in raw_ratios.items():
            d['ratios'][k] = _parse_num(v) if v is not None else None

    # Map pros/cons into pros_cons dict
    if 'pros_cons' not in d:
        d['pros_cons'] = {
            'pros': d.get('pros', []) or [],
            'cons': d.get('cons', []) or [],
        }

    def _parallel_to_rows(section_dict, key_map=None):
        """Convert {"years": [...], "sales": [...]} to [{year, sales}, ...] reversed (newest first)."""
        if not section_dict or not isinstance(section_dict, dict):
            return []
        years = section_dict.get('years', [])
        if not years:
            return []
        rows = []
        for i in range(len(years)):
            row = {'year': years[i]}
            for key, vals in section_dict.items():
                if key == 'years':
                    continue
                mapped_key = key_map.get(key, key) if key_map else key
                if isinstance(vals, list) and i < len(vals):
                    row[mapped_key] = vals[i]
            rows.append(row)
        rows.reverse()  # newest first
        return rows

    # Annual P&L -> annual_results (newest first)
    if 'annual' in d:
        d['annual_results'] = _parallel_to_rows(d['annual'])

    # Balance sheet -> balance_sheet as list of dicts (newest first)
    if 'balance_sheet' in d and isinstance(d['balance_sheet'], dict):
        d['balance_sheet_rows'] = _parallel_to_rows(d['balance_sheet'])
        # Engine uses 'balance_sheet' as list too, override
        d['balance_sheet'] = d['balance_sheet_rows']

    # Cash flow -> cash_flow as list of dicts (newest first)
    if 'cash_flow' in d and isinstance(d['cash_flow'], dict):
        cf_map = {'operating': 'cash_from_operations', 'investing': 'investing', 'financing': 'financing'}
        d['cash_flow_rows'] = _parallel_to_rows(d['cash_flow'], cf_map)
        d['cash_flow'] = d['cash_flow_rows']

    # Quarterly -> quarterly_results
    if 'quarterly' in d:
        d['quarterly_results'] = _parallel_to_rows(d['quarterly'])

    # Normalize growth_rates: parse strings to floats + add aliases for formatter
    if d.get('growth_rates') and isinstance(d['growth_rates'], dict):
        gr = {}
        for k, v in d['growth_rates'].items():
            gr[k] = _parse_num(v)
        # Add aliases: formatter expects stock_price_ttm & roe_ttm
        if 'stock_price_1y' in gr and 'stock_price_ttm' not in gr:
            gr['stock_price_ttm'] = gr['stock_price_1y']
        if 'roe_latest' in gr and 'roe_ttm' not in gr:
            gr['roe_ttm'] = gr['roe_latest']
        d['growth_rates'] = gr

    # Normalize shareholding: parallel-arrays -> list of dicts (newest first)
    # Engine expects: [{promoter_pct: x, fii_pct: y, dii_pct: z, public_pct: w, pledged_pct: p}, ...]
    # Screener returns: {quarters: [...], promoters: [...], fiis: [...], diis: [...], ...}
    sh = d.get('shareholding')
    if sh and isinstance(sh, dict) and 'quarters' in sh:
        quarters = sh.get('quarters', [])
        rows = []
        for i in range(len(quarters)):
            row = {
                'quarter': quarters[i],
                'promoter_pct': _parse_num(sh.get('promoters', [None] * (i + 1))[i] if i < len(sh.get('promoters', [])) else None),
                'fii_pct': _parse_num(sh.get('fiis', [None] * (i + 1))[i] if i < len(sh.get('fiis', [])) else None),
                'dii_pct': _parse_num(sh.get('diis', [None] * (i + 1))[i] if i < len(sh.get('diis', [])) else None),
                'public_pct': _parse_num(sh.get('public', [None] * (i + 1))[i] if i < len(sh.get('public', [])) else None),
                'pledged_pct': _parse_num(sh.get('pledged', [None] * (i + 1))[i] if i < len(sh.get('pledged', [])) else None),
            }
            rows.append(row)
        rows.reverse()  # newest first for engine
        # Keep original dict for formatter (it uses parallel-array format)
        d['shareholding_original'] = sh
        d['shareholding'] = rows

    return d


def analyze_single(symbol, config, holding=False, refresh=False, no_trendlyne=False):
    """Run full analysis on a single stock. Returns structured dict."""
    logger = logging.getLogger("stock_analyzer")
    symbol = symbol.upper().strip()

    data_quality = {}

    # 1. Fetch fundamentals from Screener.in
    logger.info("Fetching fundamentals for %s...", symbol)
    screener_obj = None
    screener_data = None
    try:
        screener_obj = fetch_screener_data(symbol, use_cache=not refresh)
        if screener_obj:
            raw_dict = _dataclass_to_dict(screener_obj)
            screener_data = _normalize_screener_for_engine(raw_dict)
            data_quality["screener"] = "ok"
        else:
            data_quality["screener"] = "no data"
    except Exception as e:
        logger.error("Screener fetch failed: %s", e)
        data_quality["screener"] = "error: %s" % e

    # 2. Fetch technicals from Kite
    logger.info("Fetching technicals for %s...", symbol)
    technical_obj = None
    technical_data = None
    try:
        technical_obj = fetch_technical_data(symbol, use_cache=not refresh)
        if technical_obj:
            technical_data = _dataclass_to_dict(technical_obj)
            data_quality["kite"] = "ok"
        else:
            data_quality["kite"] = "no data (token expired?)"
    except Exception as e:
        logger.error("Kite fetch failed: %s", e)
        data_quality["kite"] = "error: %s" % e

    # 3. Fetch Trendlyne (optional)
    trendlyne_obj = None
    trendlyne_data = None
    if not no_trendlyne:
        logger.info("Fetching Trendlyne data for %s...", symbol)
        try:
            trendlyne_obj = fetch_trendlyne_data(symbol, use_cache=not refresh)
            if trendlyne_obj:
                trendlyne_data = _dataclass_to_dict(trendlyne_obj)
                data_quality["trendlyne"] = "ok"
            else:
                data_quality["trendlyne"] = "no data"
        except Exception as e:
            logger.warning("Trendlyne fetch failed (non-critical): %s", e)
            data_quality["trendlyne"] = "skipped: %s" % e
    else:
        data_quality["trendlyne"] = "skipped by user"

    # 4. Compute scores
    scores = {}
    verdict = None
    if screener_data:
        try:
            # Fundamental scores
            piotroski = piotroski_f_score(screener_data)
            pe = screener_data.get("ratios", {}).get("Stock P/E")
            growth_3y = screener_data.get("growth_rates", {}).get("profit_3y")
            peg = peg_ratio(pe, growth_3y)
            dupont = dupont_analysis(screener_data)

            market_cap = screener_data.get("ratios", {}).get("Market Cap")
            altman = altman_z_score(screener_data, market_cap)

            fundamental_sc = compute_fundamental_score(screener_data, config)

            # Valuation score
            industry_pe = screener_data.get("ratios", {}).get("Industry PE")
            valuation_sc = compute_valuation_score(pe, peg, industry_pe, dupont)

            # Technical score (flatten nested dataclass dict for scoring engine)
            technical_sc = {"score": 50, "sub_scores": {}, "signals": ["No technical data"]}
            if technical_data:
                flat_tech = flatten_technical_data(technical_data)
                technical_sc = compute_technical_score(flat_tech, config)

            # Momentum score
            momentum_sc = compute_momentum_score(screener_data, trendlyne_data)

            # Risk score
            risk_sc = compute_risk_score(screener_data, altman)

            # Extract numeric scores for verdict
            f_num = fundamental_sc.get("score", 50)
            v_num = valuation_sc.get("score", 50)
            t_num = technical_sc.get("score", 50)
            m_num = momentum_sc.get("score", 50)
            r_num = risk_sc.get("score", 50)

            # Composite verdict
            verdict = compute_verdict(
                fundamental_score=f_num,
                valuation_score=v_num,
                technical_score=t_num,
                momentum_score=m_num,
                risk_score=r_num,
                config=config,
                screener_data=screener_data,
                altman_z=altman
            )

            scores = {
                "fundamental": fundamental_sc,
                "valuation": valuation_sc,
                "technical": technical_sc,
                "momentum": momentum_sc,
                "risk": risk_sc,
                "piotroski": piotroski,
                "peg": peg,
                "dupont": dupont,
                "altman_z": altman,
            }

        except Exception as e:
            logger.error("Scoring failed: %s", e, exc_info=True)
            data_quality["scoring"] = "error: %s" % e

    # Completeness
    sources_ok = sum(1 for v in data_quality.values() if v == "ok")
    total_sources = len([k for k in data_quality if k != "completeness_pct"])
    data_quality["completeness_pct"] = int(sources_ok / max(total_sources, 1) * 100)

    return {
        "symbol": symbol,
        "screener_data": screener_data,
        "technical_data": technical_data,
        "trendlyne_data": trendlyne_data,
        "scores": scores,
        "verdict": verdict,
        "data_quality": data_quality,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Stock Analyzer -- BUY/WAIT/EXIT/SELL verdict",
        prog="stock_analyzer"
    )
    parser.add_argument("symbols", nargs="*", help="Stock symbols (e.g., RELIANCE TCS)")
    parser.add_argument("--watchlist", type=str, help="File with symbols (one per line)")
    parser.add_argument("--json", action="store_true", help="JSON output (for Claude)")
    parser.add_argument("--holding", action="store_true", help="You are holding this stock")
    parser.add_argument("--compare", action="store_true", help="Comparison mode")
    parser.add_argument("--refresh", action="store_true", help="Force refresh (ignore cache)")
    parser.add_argument("--no-trendlyne", action="store_true", help="Skip Trendlyne")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Collect symbols
    symbols = list(args.symbols) if args.symbols else []
    if args.watchlist:
        wl_path = Path(args.watchlist)
        if wl_path.exists():
            with open(wl_path) as f:
                for line in f:
                    s = line.strip().upper()
                    if s and not s.startswith("#"):
                        symbols.append(s)
        else:
            print("Error: watchlist not found: %s" % args.watchlist, file=sys.stderr)
            sys.exit(1)

    if not symbols:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    if len(symbols) == 1 and not args.compare:
        result = analyze_single(
            symbols[0], config,
            holding=args.holding,
            refresh=args.refresh,
            no_trendlyne=args.no_trendlyne
        )
        if args.json:
            print(format_json(
                result["symbol"], result["screener_data"], result["technical_data"],
                result["trendlyne_data"], result["scores"], result["verdict"],
                result["data_quality"]
            ))
        else:
            print(format_terminal(
                result["symbol"], result["screener_data"], result["technical_data"],
                result["trendlyne_data"], result["scores"], result["verdict"],
                result["data_quality"]
            ))
    else:
        all_results = []
        for i, sym in enumerate(symbols):
            if i > 0:
                time.sleep(config["scraping"]["screener_delay_sec"])
            print("Analyzing %s (%d/%d)..." % (sym, i+1, len(symbols)), file=sys.stderr)
            result = analyze_single(
                sym, config,
                holding=args.holding,
                refresh=args.refresh,
                no_trendlyne=args.no_trendlyne
            )
            display = {
                "meta": {"symbol": sym},
                "fundamentals": {"ratios": result["screener_data"].get("ratios", {})} if result["screener_data"] else {},
                "technicals": result["technical_data"] or {},
                "verdict": result["verdict"] or {},
            }
            all_results.append(display)

            if not args.compare:
                if args.json:
                    print(format_json(
                        result["symbol"], result["screener_data"], result["technical_data"],
                        result["trendlyne_data"], result["scores"], result["verdict"],
                        result["data_quality"]
                    ))
                else:
                    print(format_terminal(
                        result["symbol"], result["screener_data"], result["technical_data"],
                        result["trendlyne_data"], result["scores"], result["verdict"],
                        result["data_quality"]
                    ))
                print()

        if args.compare:
            if args.json:
                print(format_comparison_json(all_results))
            else:
                print(format_comparison_terminal(all_results))


if __name__ == "__main__":
    main()
