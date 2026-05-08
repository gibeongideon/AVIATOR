#!/usr/bin/env bash
set -e

echo "── Creating virtual environment ──"
python3 -m venv .venv
source .venv/bin/activate

echo "── Installing Python dependencies ──"
pip install -r requirements.txt

echo "── Installing Playwright Chromium browser ──"
playwright install chromium

echo ""
echo "✓ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit config.py → set USERNAME and PASSWORD"
echo "  2. source .venv/bin/activate"
echo "  3. python inspect.py    ← verify selectors first"
echo "  4. python bot.py        ← run the bot"
