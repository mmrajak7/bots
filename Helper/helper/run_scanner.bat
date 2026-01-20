@echo off
REM Windows Task Scheduler wrapper for scanner (two-tier strategy)
REM Run EVERY MINUTE during market hours
REM Auto-detects: Full scan at :16, :31, :46 | Quick check all other minutes

cd /d C:\Users\mail2\Documents\Projects\BOTS\Helper\helper
python scanner.py

REM Optional: Add error logging
if errorlevel 1 (
    echo Scanner failed at %date% %time% >> scanner_errors.log
)
