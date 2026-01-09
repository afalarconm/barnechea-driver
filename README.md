# Lo Barnechea License Monitor

Checks Saltalá's public API for driver's license renewal availability in Lo Barnechea. If a slot is found, it automatically books it and notifies registered users via WhatsApp (Kapso).

## Setup

### 1. Kapso Account Setup

1. Create an account at [app.kapso.ai](https://app.kapso.ai)
2. Create a new project (e.g., "Barnechea Driver")
3. Go to **Settings > API Keys** and create an API key
4. Go to **WhatsApp > Phone Numbers** and set up the free digital number (note the `phone_number_id`)
5. Go to **Database > Tables** and create a table called `users` with these columns:
   - `id` (uuid, auto)
   - `phone` (text, required)
   - `rut` (text)
   - `first_name` (text)
   - `last_name` (text)
   - `email` (text)
   - `mode` (text, default: "notify")
   - `status` (text, default: "active")
   - `registered_at` (timestamp, default: now())
   - `notified_at` (timestamp)
6. Go to **WhatsApp > Templates** and create a template named `slot_available`:
   - Category: `UTILITY`
   - Language: `es` (Spanish)
   - Body: `Hay disponibilidad para renovar licencia en Lo Barnechea! Primer dia: {{1}}. Reserva: {{2}}`
   - (Templates require Meta approval, ~24-48h)

### 2. GitHub Actions

- Add secrets in GitHub → Settings → Secrets and variables → Actions:
  
  **Required for Kapso:**
  - `KAPSO_API_KEY` - Your Kapso API key
  - `KAPSO_PHONE_NUMBER_ID` - Your Kapso phone number ID
  
  **Note on auto-booking:**
  - Auto-booking uses the **Kapso `users` table** user data (`rut`, `first_name`, `last_name`, etc.). There is no environment-variable fallback.
  
- The workflow at `.github/workflows/check.yml` runs every 5 minutes and can be dispatched manually.

## Configuration (Environment Variables)

The script is configured via environment variables.

### Kapso
- `KAPSO_API_KEY` - Required for WhatsApp notifications and database access
- `KAPSO_PHONE_NUMBER_ID` - Required for sending WhatsApp messages

### API & Service
- `SALTALA_BASE` (default `https://saltala.apisaltala.com/api/v1`)
- `PUBLIC_URL` (default `lobarnechea`)
- `TARGET_LINE_NAMES` (default `Renovación`)
- `FALLBACK_LINE_ID` (default `1768`)
- `UNIT_HINT` (default `277`)
- `NUMBER_OF_MONTH` (default `2`)
- `TZ_NAME` (default `America/Santiago`) - Used to compute the correct timezone offset per date (DST-safe).
- `TZ_OFFSET` (optional) - Manual override like `-03:00` (takes precedence over `TZ_NAME`).

Note: Some Saltalá deployments include `patientRut=<digits>` in availability requests. This script derives it automatically from the first available Kapso user's `rut` (digits-only) when present.

### Mocking (for local testing)
- `MOCK_LINE_ID`, `MOCK_LINE_NAME`, `MOCK_DAYS`, `MOCK_TIMES`

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set Kapso credentials
export KAPSO_API_KEY="your-api-key"
export KAPSO_PHONE_NUMBER_ID="your-phone-number-id"

# Run the checker
python src/check_lobarnechea.py
```

## How It Works

1. **User Registration**: Users send a message to your Kapso WhatsApp number to register
2. **Slot Monitoring**: The script runs every 5 minutes via GitHub Actions, checking for available slots
3. **Auto-booking**: If a slot is found and there are registered users with `mode="autobook"`, it attempts to book for the first user (FIFO)
4. **Notifications**: All active users are notified via WhatsApp when slots are found
5. **Follow-up**: Users marked as "pending" receive a follow-up message after 1 hour asking if they completed their booking

## Landing Page

The `docs/` folder contains a static landing page for the service. It's designed to be deployed via GitHub Pages.

### Deploy to GitHub Pages

1. Go to **Settings > Pages** in your GitHub repository
2. Under "Source", select **Deploy from a branch**
3. Select the `main` branch and `/docs` folder
4. Click **Save**

Your site will be available at `https://yourusername.github.io/barnechea-driver/`

## License

MIT
