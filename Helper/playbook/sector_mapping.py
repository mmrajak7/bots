"""Stock-to-sector mapping for backtest.

Maps stock symbols to their Nifty sector index.
Stocks not in any sector index map to 'other' (use NIFTY 50 regime).

Sources (March 2026):
  - NSE Indices factsheets (niftyindices.com)
  - smart-investing.in index weightage pages
  - Zerodha Markets constituent lists
  - Screener.in index pages

When a stock appears in multiple indices, the MORE SPECIFIC index wins:
  - SBIN -> nifty_psu_bank (not nifty_bank)
  - HDFCBANK -> nifty_pvt_bank (not nifty_bank)
  - RELIANCE -> nifty_energy (not nifty_infra)
  - NTPC -> nifty_energy (not nifty_infra)
  - DLF -> nifty_realty (not nifty_infra)
"""

# Maps stock symbol -> sector key (matches _sector_{key}.json filename)
STOCK_SECTOR = {
    # =========================================================================
    # NIFTY IT (10 stocks)
    # Source: smart-investing.in, Mar 2026
    # =========================================================================
    "TCS": "nifty_it",
    "INFY": "nifty_it",
    "HCLTECH": "nifty_it",
    "WIPRO": "nifty_it",
    "TECHM": "nifty_it",
    "LTIM": "nifty_it",           # LTIMindtree
    "PERSISTENT": "nifty_it",
    "OFSS": "nifty_it",           # Oracle Financial Services
    "MPHASIS": "nifty_it",
    "COFORGE": "nifty_it",

    # IT-adjacent (not in Nifty IT index but clearly IT sector)
    "LTTS": "nifty_it",           # L&T Technology Services
    "KPITTECH": "nifty_it",
    "TATAELXSI": "nifty_it",
    "CYIENT": "nifty_it",
    "MASTEK": "nifty_it",
    "BSOFT": "nifty_it",          # Birlasoft
    "INTELLECT": "nifty_it",      # Intellect Design Arena
    "ECLERX": "nifty_it",
    "SASKEN": "nifty_it",
    "SONATSOFTW": "nifty_it",     # Sonata Software
    "DATAPATTNS": "nifty_it",     # Data Patterns
    "RATEGAIN": "nifty_it",
    "NETWEB": "nifty_it",
    "CIGNITITEC": "nifty_it",     # Cigniti Technologies
    "INFOBEAN": "nifty_it",
    "ONMOBILE": "nifty_it",
    "SAKSOFT": "nifty_it",
    "EMUDHRA": "nifty_it",
    "MAPMYINDIA": "nifty_it",
    "CMSINFO": "nifty_it",
    "NAUKRI": "nifty_it",         # Info Edge (Naukri) - internet/IT
    "JUSTDIAL": "nifty_it",
    "APTECHT": "nifty_it",
    "QUICKHEAL": "nifty_it",
    "COMPUSOFT": "nifty_it",
    "ONWARDTEC": "nifty_it",

    # =========================================================================
    # NIFTY PSU BANK (12 stocks) -- more specific than nifty_bank
    # Source: smart-investing.in, Mar 2026
    # =========================================================================
    "SBIN": "nifty_psu_bank",
    "BANKBARODA": "nifty_psu_bank",
    "UNIONBANK": "nifty_psu_bank",
    "PNB": "nifty_psu_bank",
    "CANBK": "nifty_psu_bank",
    "INDIANB": "nifty_psu_bank",      # Indian Bank
    "BANKINDIA": "nifty_psu_bank",     # Bank of India
    "IOB": "nifty_psu_bank",           # Indian Overseas Bank
    "MAHABANK": "nifty_psu_bank",      # Bank of Maharashtra
    "UCOBANK": "nifty_psu_bank",
    "CENTRALBK": "nifty_psu_bank",
    "PSB": "nifty_psu_bank",           # Punjab & Sind Bank

    # PSU bank adjacent
    "J&KBANK": "nifty_psu_bank",       # J&K Bank (govt-owned)

    # =========================================================================
    # NIFTY PRIVATE BANK (10 stocks) -- more specific than nifty_bank
    # Source: smart-investing.in, Mar 2026
    # =========================================================================
    "HDFCBANK": "nifty_pvt_bank",
    "ICICIBANK": "nifty_pvt_bank",
    "AXISBANK": "nifty_pvt_bank",
    "KOTAKBANK": "nifty_pvt_bank",
    "INDUSINDBK": "nifty_pvt_bank",
    "FEDERALBNK": "nifty_pvt_bank",
    "YESBANK": "nifty_pvt_bank",
    "IDFCFIRSTB": "nifty_pvt_bank",
    "BANDHANBNK": "nifty_pvt_bank",
    "RBLBANK": "nifty_pvt_bank",

    # Private bank adjacent
    "AUBANK": "nifty_pvt_bank",        # AU Small Finance Bank
    "CUB": "nifty_pvt_bank",           # City Union Bank
    "KVB": "nifty_pvt_bank",           # Karur Vysya Bank
    "KTKBANK": "nifty_pvt_bank",       # Karnataka Bank
    "CSBBANK": "nifty_pvt_bank",       # CSB Bank
    "EQUITASBNK": "nifty_pvt_bank",    # Equitas Small Finance Bank
    "UJJIVANSFB": "nifty_pvt_bank",    # Ujjivan Small Finance Bank

    # =========================================================================
    # NIFTY BANK (14 stocks) -- for banks not in PSU or PVT specific indices
    # Source: smart-investing.in, Mar 2026
    # Note: Most banks already mapped to PSU/PVT bank above.
    #       Remaining mapped to nifty_bank only if not in PSU/PVT.
    # =========================================================================
    # (All major banks already classified above as PSU or PVT bank.)
    # NBFC/financial companies that are bank-adjacent:
    "LICHSGFIN": "nifty_bank",
    "POONAWALLA": "nifty_bank",        # Poonawalla Fincorp
    "SBICARD": "nifty_bank",
    "SBILIFE": "nifty_bank",
    "ICICIGI": "nifty_bank",           # ICICI Lombard GIC
    "ICICIPRULI": "nifty_bank",        # ICICI Prudential Life
    "LICI": "nifty_bank",              # LIC
    "ANGELONE": "nifty_bank",
    "CAMS": "nifty_bank",
    "CDSL": "nifty_bank",
    "MCX": "nifty_bank",
    "IEX": "nifty_bank",
    "KFINTECH": "nifty_bank",
    "CREDITACC": "nifty_bank",
    "JMFINANCIL": "nifty_bank",
    "MOTILALOFS": "nifty_bank",

    # =========================================================================
    # NIFTY PHARMA (20 stocks)
    # Source: smart-investing.in, Mar 2026
    # =========================================================================
    "SUNPHARMA": "nifty_pharma",
    "DIVISLAB": "nifty_pharma",
    "TORNTPHARM": "nifty_pharma",
    "DRREDDY": "nifty_pharma",
    "LUPIN": "nifty_pharma",
    "CIPLA": "nifty_pharma",
    "ZYDUSLIFE": "nifty_pharma",
    "MANKIND": "nifty_pharma",          # Mankind Pharma
    "AUROPHARMA": "nifty_pharma",
    "ALKEM": "nifty_pharma",
    "BIOCON": "nifty_pharma",
    "GLENMARK": "nifty_pharma",
    "ABBOTTINDIA": "nifty_pharma",
    "LAURUSLABS": "nifty_pharma",
    "IPCALAB": "nifty_pharma",
    "AJANTPHARM": "nifty_pharma",
    "JBCHEPHARM": "nifty_pharma",
    "GLAND": "nifty_pharma",            # Gland Pharma
    "WOCKPHARMA": "nifty_pharma",        # Wockhardt
    "PIRAMALPHAR": "nifty_pharma",       # Piramal Pharma

    # Pharma adjacent (not in index but clearly pharma sector)
    "NATCOPHARM": "nifty_pharma",
    "GRANULES": "nifty_pharma",
    "SYNGENE": "nifty_pharma",
    "APLLTD": "nifty_pharma",           # Alembic Pharmaceuticals
    "SPARC": "nifty_pharma",            # Sun Pharma Advanced Research
    "PPLPHARMA": "nifty_pharma",
    "INDOCO": "nifty_pharma",
    "FDC": "nifty_pharma",
    "LALPATHLAB": "nifty_pharma",
    "METROPOLIS": "nifty_pharma",
    "APOLLOHOSP": "nifty_pharma",
    "MEDANTA": "nifty_pharma",
    "SHALBY": "nifty_pharma",
    "YATHARTH": "nifty_pharma",
    "KRSNAA": "nifty_pharma",
    "MARKSANS": "nifty_pharma",
    "SMSPHARMA": "nifty_pharma",
    "HESTERBIO": "nifty_pharma",
    "SHILPAMED": "nifty_pharma",
    "MOREPENLAB": "nifty_pharma",
    "NECLIFE": "nifty_pharma",
    "AARTIDRUGS": "nifty_pharma",
    "AAREYDRUGS": "nifty_pharma",
    "POLYMED": "nifty_pharma",
    "UNICHEMLAB": "nifty_pharma",
    "INDSWFTLAB": "nifty_pharma",
    "ARTEMISMED": "nifty_pharma",
    "COHANCE": "nifty_pharma",
    "GLAXO": "nifty_pharma",
    "ZYDUSWELL": "nifty_pharma",

    # =========================================================================
    # NIFTY AUTO (15 stocks)
    # Source: smart-investing.in, Mar 2026
    # =========================================================================
    "MARUTI": "nifty_auto",
    "M&M": "nifty_auto",               # Mahindra & Mahindra
    "BAJAJ-AUTO": "nifty_auto",
    "EICHERMOT": "nifty_auto",
    "TVSMOTOR": "nifty_auto",
    "MOTHERSON": "nifty_auto",          # Samvardhana Motherson
    "TATAMOTORS": "nifty_auto",
    "HEROMOTOCO": "nifty_auto",
    "ASHOKLEY": "nifty_auto",           # Ashok Leyland
    "BOSCHLTD": "nifty_auto",
    "BHARATFORG": "nifty_auto",
    "EXIDEIND": "nifty_auto",
    "TIINDIA": "nifty_auto",            # Tube Investments of India
    "MRF": "nifty_auto",
    "SONACOMS": "nifty_auto",           # Sona BLW Precision

    # Auto adjacent
    "BALKRISIND": "nifty_auto",
    "ESCORTS": "nifty_auto",
    "ENDURANCE": "nifty_auto",
    "JAMNAAUTO": "nifty_auto",
    "GABRIEL": "nifty_auto",
    "SANSERA": "nifty_auto",
    "SUNDRMFAST": "nifty_auto",
    "SUPRAJIT": "nifty_auto",
    "NIPPOBATRY": "nifty_auto",
    "SHARDAMOTR": "nifty_auto",
    "AUTOIND": "nifty_auto",
    "AUTOAXLES": "nifty_auto",
    "RICOAUTO": "nifty_auto",
    "SANDHAR": "nifty_auto",
    "ATULAUTO": "nifty_auto",
    "MUNJALAU": "nifty_auto",
    "MUNJALSHOW": "nifty_auto",
    "APOLLOTYRE": "nifty_auto",
    "JKTYRE": "nifty_auto",
    "TVSHLTD": "nifty_auto",            # TVS Holdings
    "TVSSRICHAK": "nifty_auto",
    "NDRAUTO": "nifty_auto",
    "JTEKTINDIA": "nifty_auto",
    "BHARATGEAR": "nifty_auto",
    "PRITIKAUTO": "nifty_auto",

    # =========================================================================
    # NIFTY METAL (15 stocks)
    # Source: smart-investing.in, Mar 2026
    # =========================================================================
    "JSWSTEEL": "nifty_metal",
    "VEDL": "nifty_metal",              # Vedanta
    "ADANIENT": "nifty_metal",          # Adani Enterprises
    "TATASTEEL": "nifty_metal",
    "HINDZINC": "nifty_metal",
    "HINDALCO": "nifty_metal",
    "JINDALSTEL": "nifty_metal",        # Jindal Steel & Power
    "NATIONALUM": "nifty_metal",        # NALCO
    "NMDC": "nifty_metal",
    "LLOYDSME": "nifty_metal",          # Lloyds Metals & Energy
    "SAIL": "nifty_metal",
    "JSL": "nifty_metal",               # Jindal Stainless
    "WELSPUNLIV": "nifty_metal",        # Welspun Corp
    "COALINDIA": "nifty_metal",
    "APLAPOLLO": "nifty_metal",         # APL Apollo Tubes

    # Metal adjacent
    "MOIL": "nifty_metal",
    "HINDCOPPER": "nifty_metal",
    "RATNAMANI": "nifty_metal",
    "KIOCL": "nifty_metal",
    "JINDALSAW": "nifty_metal",
    "GPIL": "nifty_metal",              # Godawari Power & Ispat
    "JAIBALAJI": "nifty_metal",
    "PRAKASHSTL": "nifty_metal",
    "RAIN": "nifty_metal",
    "GOACARBON": "nifty_metal",
    "MAHSEAMLES": "nifty_metal",
    "IFGLEXPOR": "nifty_metal",
    "MUKANDLTD": "nifty_metal",

    # =========================================================================
    # NIFTY FMCG (15 stocks)
    # Source: smart-investing.in, Mar 2026
    # =========================================================================
    "HINDUNILVR": "nifty_fmcg",
    "ITC": "nifty_fmcg",
    "NESTLEIND": "nifty_fmcg",
    "VBL": "nifty_fmcg",               # Varun Beverages
    "BRITANNIA": "nifty_fmcg",
    "GODREJCP": "nifty_fmcg",
    "TATACONSUM": "nifty_fmcg",
    "MARICO": "nifty_fmcg",
    "UNITDSPR": "nifty_fmcg",          # United Spirits
    "DABUR": "nifty_fmcg",
    "COLPAL": "nifty_fmcg",
    "PATANJALI": "nifty_fmcg",         # Patanjali Foods
    "UBL": "nifty_fmcg",               # United Breweries
    "RADICO": "nifty_fmcg",            # Radico Khaitan
    "EMAMILTD": "nifty_fmcg",
    "PGHH": "nifty_fmcg",              # P&G Hygiene

    # FMCG adjacent
    "JYOTHYLAB": "nifty_fmcg",
    "VSTIND": "nifty_fmcg",            # VST Industries (tobacco)
    "PAGEIND": "nifty_fmcg",           # Page Industries (innerwear FMCG-like)
    "GODFRYPHLP": "nifty_fmcg",
    "BIKAJI": "nifty_fmcg",
    "BECTORFOOD": "nifty_fmcg",
    "HERITGFOOD": "nifty_fmcg",
    "PARAGMILK": "nifty_fmcg",

    # =========================================================================
    # NIFTY ENERGY (10 stocks)
    # Source: smart-investing.in, Mar 2026
    # Note: This index includes oil/gas, power, and related companies
    # =========================================================================
    "RELIANCE": "nifty_energy",
    "NTPC": "nifty_energy",
    "ONGC": "nifty_energy",
    "POWERGRID": "nifty_energy",
    "ADANIPOWER": "nifty_energy",
    "IOC": "nifty_energy",              # Indian Oil Corporation
    "BPCL": "nifty_energy",
    "ADANIGREEN": "nifty_energy",
    "GAIL": "nifty_energy",
    "TATAPOWER": "nifty_energy",
    "OIL": "nifty_energy",              # Oil India
    "ADANIENSO": "nifty_energy",        # Adani Energy Solutions

    # Energy adjacent (power, oil & gas, renewables)
    "TORNTPOWER": "nifty_energy",
    "CESC": "nifty_energy",
    "SUZLON": "nifty_energy",
    "MRPL": "nifty_energy",
    "CHENNPETRO": "nifty_energy",
    "RECLTD": "nifty_energy",
    "PTC": "nifty_energy",
    "HUDCO": "nifty_energy",
    "PGEL": "nifty_energy",
    "GUJGASLTD": "nifty_energy",
    "MGL": "nifty_energy",              # Mahanagar Gas
    "GSPL": "nifty_energy",
    "HFCL": "nifty_energy",
    "GULFPETRO": "nifty_energy",
    "FACT": "nifty_energy",
    "RCF": "nifty_energy",
    "NFL": "nifty_energy",
    "DEEPAKFERT": "nifty_energy",
    "GSFC": "nifty_energy",
    "INOXGREEN": "nifty_energy",
    "SWSOLAR": "nifty_energy",
    "KPIGREEN": "nifty_energy",
    "RTNPOWER": "nifty_energy",
    "CONFIPET": "nifty_energy",
    "SPLPETRO": "nifty_energy",

    # =========================================================================
    # NIFTY REALTY (10 stocks)
    # Source: smart-investing.in, Feb 2026
    # =========================================================================
    "DLF": "nifty_realty",
    "LODHA": "nifty_realty",            # Macrotech Developers
    "PRESTIGE": "nifty_realty",
    "PHOENIXLTD": "nifty_realty",
    "GODREJPROP": "nifty_realty",
    "OBEROIRLTY": "nifty_realty",
    "BRIGADE": "nifty_realty",
    "ANANTRAJ": "nifty_realty",
    "SOBHA": "nifty_realty",
    "RAYMOND": "nifty_realty",          # Raymond (realty index constituent 2026)

    # Realty adjacent
    "KOLTEPATIL": "nifty_realty",
    "DBREALTY": "nifty_realty",
    "HUBTOWN": "nifty_realty",
    "HEMIPROP": "nifty_realty",
    "TARC": "nifty_realty",
    "PURVA": "nifty_realty",            # Puravankara
    "MAHLIFE": "nifty_realty",
    "SUNTECK": "nifty_realty",
    "SMLT": "nifty_realty",             # Signatureglobal
    "PENINLAND": "nifty_realty",

    # =========================================================================
    # NIFTY INFRA (30 stocks)
    # Source: smart-investing.in, Feb 2026
    # Note: Many Infra stocks already mapped to more specific sectors above.
    #       Only stocks NOT in other sector indices go here.
    # =========================================================================
    "LT": "nifty_infra",               # Larsen & Toubro
    "ULTRACEMCO": "nifty_infra",
    "ADANIPORTS": "nifty_infra",
    "GRASIM": "nifty_infra",
    "AMBUJACEM": "nifty_infra",
    "ACC": "nifty_infra",
    "INDIGO": "nifty_infra",            # InterGlobe Aviation
    "CUMMINSIND": "nifty_infra",
    "SIEMENS": "nifty_infra",
    "ABB": "nifty_infra",
    "HAVELLS": "nifty_infra",
    "BHARTIARTL": "nifty_infra",        # Bharti Airtel
    "CONCOR": "nifty_infra",
    "BHEL": "nifty_infra",
    "POLYCAB": "nifty_infra",

    # Infra adjacent (construction, engineering, cement, capital goods)
    "IRCTC": "nifty_infra",
    "IRCON": "nifty_infra",
    "RVNL": "nifty_infra",
    "RITES": "nifty_infra",
    "NCC": "nifty_infra",
    "KEC": "nifty_infra",
    "HGINFRA": "nifty_infra",
    "PNCINFRA": "nifty_infra",
    "KNRCON": "nifty_infra",
    "ASHOKA": "nifty_infra",            # Ashoka Buildcon
    "HCC": "nifty_infra",
    "GRINFRA": "nifty_infra",
    "GPTINFRA": "nifty_infra",
    "RPPINFRA": "nifty_infra",
    "MANINFRA": "nifty_infra",
    "PATELENG": "nifty_infra",
    "MONTECARLO": "nifty_infra",
    "HAL": "nifty_infra",
    "COCHINSHIP": "nifty_infra",
    "GRSE": "nifty_infra",
    "BEML": "nifty_infra",
    "MIDHANI": "nifty_infra",
    "CROMPTON": "nifty_infra",
    "VOLTAS": "nifty_infra",
    "THERMAX": "nifty_infra",
    "ELECON": "nifty_infra",
    "ELGIEQUIP": "nifty_infra",
    "KEI": "nifty_infra",
    "ISGEC": "nifty_infra",
    "TITAGARH": "nifty_infra",
    "JKCEMENT": "nifty_infra",
    "RAMCOCEM": "nifty_infra",
    "SHREECEM": "nifty_infra",
    "BIRLACORPN": "nifty_infra",
    "SHREDIGCEM": "nifty_infra",
    "ORIENTCEM": "nifty_infra",
    "DMART": "nifty_infra",             # Avenue Supermarts (retail, but in infra-like large caps)
    "DIXON": "nifty_infra",
    "KAYNES": "nifty_infra",
    "SYRMA": "nifty_infra",
    "ASTRAL": "nifty_infra",
    "SUPREMEIND": "nifty_infra",
    "PRINCEPIPE": "nifty_infra",
    "APOLLOPIPE": "nifty_infra",
    "FINPIPE": "nifty_infra",
    "FINCABLES": "nifty_infra",
    "RAILTEL": "nifty_infra",
    "TEJASNET": "nifty_infra",
    "STOVEKRAFT": "nifty_infra",
    "TIMKEN": "nifty_infra",
    "SKFINDIA": "nifty_infra",
    "NRBBEARING": "nifty_infra",
    "GRINDWELL": "nifty_infra",
    "CARBORUNIV": "nifty_infra",
    "CERA": "nifty_infra",
    "KAJARIACER": "nifty_infra",
    "SWELECTES": "nifty_infra",
    "POWERMECH": "nifty_infra",
    "SEPC": "nifty_infra",
    "WINDMACHIN": "nifty_infra",
    "LINDEINDIA": "nifty_infra",
    "HONAUT": "nifty_infra",
    "PIIND": "nifty_infra",
    "RALLIS": "nifty_infra",
    "DEEPAKNTR": "nifty_infra",
    "EPL": "nifty_infra",

    # =========================================================================
    # NIFTY MEDIA (15 stocks)
    # Source: smart-investing.in, Zerodha, Mar 2026
    # =========================================================================
    "SUNTV": "nifty_media",
    "PRIMEFOCUS": "nifty_media",
    "PVRINOX": "nifty_media",
    "NAZARA": "nifty_media",
    "ZEEL": "nifty_media",              # Zee Entertainment
    "TIPSMUSIC": "nifty_media",
    "SAREGAMA": "nifty_media",
    "NETWORK18": "nifty_media",
    "DBCORP": "nifty_media",
    "HATHWAY": "nifty_media",
    "DEN": "nifty_media",
    "TV18BRDCST": "nifty_media",

    # Media adjacent
    "JAGRAN": "nifty_media",
    "ENIL": "nifty_media",              # Entertainment Network India
    "RADIOCITY": "nifty_media",
    "RAJTV": "nifty_media",
    "HTMEDIA": "nifty_media",
    "DGCONTENT": "nifty_media",
    "BAGFILMS": "nifty_media",
    "CINELINE": "nifty_media",
}


def get_sector(symbol: str) -> str:
    """Return sector key for a stock. 'other' if not mapped."""
    return STOCK_SECTOR.get(symbol.upper(), "other")


def get_all_sectors() -> list:
    """Return sorted list of all unique sector keys."""
    return sorted(set(STOCK_SECTOR.values()))


def get_stocks_for_sector(sector: str) -> list:
    """Return sorted list of all stocks mapped to a given sector."""
    return sorted(sym for sym, sec in STOCK_SECTOR.items() if sec == sector)


def summary() -> dict:
    """Return count of stocks per sector."""
    counts = {}
    for sec in STOCK_SECTOR.values():
        counts[sec] = counts.get(sec, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    print("Stock-to-Sector Mapping Summary")
    print("=" * 45)
    s = summary()
    total = 0
    for sector, count in s.items():
        print(f"  {sector:20s}: {count:3d} stocks")
        total += count
    print(f"  {'TOTAL':20s}: {total:3d} stocks")
    print(f"\n  Unmapped stocks use 'other' (NIFTY 50 regime)")
