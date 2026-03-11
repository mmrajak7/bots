"""
stock_analyzer — Comprehensive Indian stock analysis tool.

Usage:
    python -m stock_analyzer RELIANCE                 # Single stock, terminal output
    python -m stock_analyzer RELIANCE --json           # JSON output for Claude
    python -m stock_analyzer RELIANCE --holding        # Analysis with EXIT recommendation
    python -m stock_analyzer RELIANCE TCS --compare    # Multi-stock comparison
    python -m stock_analyzer --watchlist stocks.txt     # Batch from file

Slash command:
    /analyze RELIANCE
    /analyze RELIANCE --holding
"""

__version__ = "1.0.0"
