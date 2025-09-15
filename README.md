# Lo Barnechea License Monitor

Checks Saltalá’s public API for driver’s license renewal availability in Lo Barnechea. If a slot is found, it automatically books it and notifies you via Telegram.

## GitHub Actions

- Add secrets in GitHub → Settings → Secrets and variables → Actions:
  
  **Required for booking:**
  - `USER_RUT`
  - `USER_FIRST_NAME`
  - `USER_LAST_NAME`
  
  **Optional for booking (but recommended):**
  - `USER_EMAIL`
  - `USER_PHONE` (Chilean number without country code, e.g., `912345678`)

  **Required for notifications:**
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
  
- The workflow at `.github/workflows/check.yml` runs on a schedule and can be dispatched manually.

## Configuration (Environment Variables)

The script is configured via environment variables. The most important ones are the user data secrets for booking.

### User Data
- `USER_RUT` (e.g., `12345678-9`)
- `USER_FIRST_NAME`
- `USER_LAST_NAME`
- `USER_EMAIL` (optional)
- `USER_PHONE` (optional)

### API & Service
- `SALTALA_BASE` (default `https://saltala.apisaltala.com/api/v1`)
- `PUBLIC_URL` (default `lobarnechea`)
- `TARGET_LINE_NAMES` (default `Renovación`)
- `FALLBACK_LINE_ID` (default `1768`)
- `UNIT_HINT` (default `277`)
- `NUMBER_OF_MONTH` (default `2`)

### Mocking (for local testing)
- `MOCK_LINE_ID`, `MOCK_LINE_NAME`, `MOCK_DAYS`, `MOCK_TIMES`

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set user data for booking
export USER_RUT="12345678-9"
export USER_FIRST_NAME="John"
export USER_LAST_NAME="Doe"
# export USER_EMAIL="john.doe@example.com" # optional
# export USER_PHONE="912345678" # optional

# Set Telegram credentials
export TELEGRAM_BOT_TOKEN="xxx"
export TELEGRAM_CHAT_ID="123456789"

# Run the checker
python src/check_lobarnechea.py
```

## License

MIT
