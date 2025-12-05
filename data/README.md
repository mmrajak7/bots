# Shared Data Folder

This folder contains shared resources used by all bots:

- `trading.db` - Shared SQLite database (positions filtered by bot_instance_id)
- `enctoken.txt` - Zerodha session token (shared across all bots)
- `signals_*.csv` - Signal files per bot
- `ignore_list_*.csv` - Ignore lists per bot

## Important Notes

1. All bots share the same database but filter by `bot_instance_id`
2. Database schema includes `bot_instance_id` column in all trading tables
3. Each bot only manages its own positions/orders
4. Zerodha token is shared (same account)
