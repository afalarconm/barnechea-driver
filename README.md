# Lo Barnechea License Monitor

Checks Saltalá’s public API for driver’s license renewal availability in Lo Barnechea and notifies via Telegram. Runs every 15 minutes with GitHub Actions.

## GitHub Actions

- Add secrets in GitHub → Settings → Secrets and variables → Actions:
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
- Workflow at `.github/workflows/check.yml` runs on schedule and on manual dispatch.

## Configuration (env)

- `SALTALA_BASE` (default `https://saltala.apisaltala.com/api/v1`)
- `PUBLIC_URL` (default `lobarnechea`)
- `TARGET_LINE_NAMES` (default `Renovación`)
- `FALLBACK_LINE_ID` (default `1768`)
- `UNIT_HINT` (default `277`)
- `NUMBER_OF_MONTH` (default `2`)

Optional (mock testing):
- `MOCK_LINE_ID`, `MOCK_LINE_NAME`, `MOCK_DAYS`, `MOCK_TIMES`

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install requests python-dotenv

# (optional) Telegram
export TELEGRAM_BOT_TOKEN="xxx" # Your Telegram bot token
export TELEGRAM_CHAT_ID="123456789" # Your Telegram chat ID

# run
python src/check_lobarnechea.py

# mock example
export MOCK_LINE_ID=12345 \
  MOCK_LINE_NAME="Renovación" \
  MOCK_DAYS="2025-10-01, 2025-10-02" \
  MOCK_TIMES="09:00, 10:00, 11:00"
python src/check_lobarnechea.py
```

## License

MIT
