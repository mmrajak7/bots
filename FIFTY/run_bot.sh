#!/bin/bash
# FIFTY Bot Runner Script
# This script ensures the logs directory exists before running the bot
# Use this in cron instead of directly calling python main.py

cd "$(dirname "$0")"

# Ensure logs directory exists BEFORE any redirection
mkdir -p logs

# Run the bot with proper logging
python main.py >> logs/cron.log 2>&1
