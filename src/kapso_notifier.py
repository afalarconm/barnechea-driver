import os
import logging
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime

KAPSO_API_KEY = os.getenv("KAPSO_API_KEY", "")
KAPSO_PHONE_NUMBER_ID = os.getenv("KAPSO_PHONE_NUMBER_ID", "")
KAPSO_BASE_URL = "https://api.kapso.ai"
KAPSO_TEMPLATE_LANGUAGE_CODE = os.getenv("KAPSO_TEMPLATE_LANGUAGE_CODE", "es_AR")
# Optional: comma-separated parameter names if your template uses NAMED params
# e.g. "day" or "first_day"
KAPSO_TEMPLATE_PARAM_NAMES = [s.strip() for s in os.getenv("KAPSO_TEMPLATE_PARAM_NAMES", "").split(",") if s.strip()]

TIMEOUT = (10, 20)

def _parse_iso_datetime(value: Any) -> datetime:
    """
    Best-effort ISO8601 parser used for FIFO ordering.
    Unknown / invalid values sort last (datetime.max).
    Accepts strings like "2026-01-12T12:34:56Z".
    """
    if not isinstance(value, str) or not value:
        return datetime.max
    try:
        # Python doesn't accept trailing "Z" in fromisoformat; convert to +00:00.
        v = value.replace("Z", "+00:00")
        return datetime.fromisoformat(v)
    except Exception:
        return datetime.max

def _normalize_whatsapp_to(to_phone: str) -> str:
    """
    WhatsApp Cloud API expects `to` as digits-only phone number in international format.
    E.g. "+56 9 1234 5678" -> "56912345678"
    """
    if not to_phone:
        return ""
    # keep digits only; strip +, spaces, hyphens, etc.
    digits = "".join(ch for ch in str(to_phone) if ch.isdigit())
    # handle "00<country><number>" style
    if digits.startswith("00"):
        digits = digits[2:]
    # Basic E.164 sanity check (WhatsApp Cloud expects international numbers with country code).
    # E.164 allows up to 15 digits; in practice WhatsApp `to` should be 10-15 digits.
    if not (10 <= len(digits) <= 15):
        return ""
    return digits

def _headers() -> Dict[str, str]:
    return {
        "X-API-Key": KAPSO_API_KEY,
        "Content-Type": "application/json",
    }

def send_whatsapp_message(to_phone: str, text: str) -> bool:
    """Send a text message to a WhatsApp number."""
    to_phone_norm = _normalize_whatsapp_to(to_phone)
    if not to_phone_norm:
        logging.error(f"Invalid/empty phone for WhatsApp send: raw={to_phone!r}")
        return False

    if not KAPSO_API_KEY or not KAPSO_PHONE_NUMBER_ID:
        logging.info(f"[Mock] WhatsApp to {to_phone_norm}: {text}")
        return True
    
    url = f"{KAPSO_BASE_URL}/meta/whatsapp/v24.0/{KAPSO_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone_norm,
        "type": "text",
        "text": {"body": text}
    }
    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        logging.info(f"WhatsApp sent to {to_phone_norm}")
        return True
    except requests.HTTPError as e:
        resp_text = (e.response.text[:500] if e.response is not None and e.response.text else "")
        logging.error(f"WhatsApp error to {to_phone_norm}: {e} {resp_text}")
        return False
    except Exception as e:
        logging.error(f"WhatsApp error to {to_phone_norm}: {e}")
        return False

def send_template_message(to_phone: str, template_name: str, params: List[str]) -> bool:
    """Send a template message (for business-initiated conversations)."""
    to_phone_norm = _normalize_whatsapp_to(to_phone)
    if not to_phone_norm:
        logging.error(f"Invalid/empty phone for template send: raw={to_phone!r}")
        return False

    if not KAPSO_API_KEY or not KAPSO_PHONE_NUMBER_ID:
        logging.info(f"[Mock] Template {template_name} to {to_phone_norm}: {params}")
        return True
    
    url = f"{KAPSO_BASE_URL}/meta/whatsapp/v24.0/{KAPSO_PHONE_NUMBER_ID}/messages"

    body_params: List[Dict[str, Any]] = []
    for idx, p in enumerate(params):
        entry: Dict[str, Any] = {"type": "text", "text": p}
        # If template was created with NAMED parameters, include parameterName when available.
        if idx < len(KAPSO_TEMPLATE_PARAM_NAMES):
            entry["parameterName"] = KAPSO_TEMPLATE_PARAM_NAMES[idx]
        body_params.append(entry)

    components = [{"type": "body", "parameters": body_params}]
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone_norm,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": KAPSO_TEMPLATE_LANGUAGE_CODE},
            "components": components
        }
    }
    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        logging.info(f"Template {template_name} sent to {to_phone_norm}")
        return True
    except requests.HTTPError as e:
        resp_text = (e.response.text[:500] if e.response is not None and e.response.text else "")
        logging.error(f"Template error to {to_phone_norm}: {e} {resp_text}")
        return False
    except Exception as e:
        logging.error(f"Template error to {to_phone_norm}: {e}")
        return False

def get_active_users() -> List[Dict[str, Any]]:
    """Fetch all users with status='active' from Kapso database."""
    if not KAPSO_API_KEY:
        logging.info("[Mock] No API key, returning empty user list")
        return []
    
    url = f"{KAPSO_BASE_URL}/platform/v1/db/users"
    params = {"status": "eq.active", "order": "registered_at.asc"}
    try:
        r = requests.get(url, params=params, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        users = r.json().get("data", [])
        if isinstance(users, list):
            # Defensive FIFO ordering: do not rely solely on API-side ordering.
            # Sort earliest registrations first; unknown/missing timestamps go last.
            users = sorted(
                users,
                key=lambda u: (
                    _parse_iso_datetime((u or {}).get("registered_at")),
                    str((u or {}).get("id", "")),
                ),
            )
        return users
    except Exception as e:
        logging.error(f"Error fetching users: {e}")
        return []

def get_pending_users_for_followup(hours: int = 1) -> List[Dict[str, Any]]:
    """Fetch users pending for more than X hours."""
    if not KAPSO_API_KEY:
        return []
    
    url = f"{KAPSO_BASE_URL}/platform/v1/db/users"
    # Query pending users - notified_at filtering will be done in Python
    params = {"status": "eq.pending", "order": "notified_at.asc"}
    try:
        r = requests.get(url, params=params, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        users = r.json().get("data", [])
        # Filter by time in Python (Kapso may not support timestamp math in query)
        cutoff = datetime.utcnow().timestamp() - (hours * 3600)
        return [u for u in users if u.get("notified_at") and 
                datetime.fromisoformat(u["notified_at"].replace("Z", "")).timestamp() < cutoff]
    except Exception as e:
        logging.error(f"Error fetching pending users: {e}")
        return []

def update_user_status(user_id: str, status: str, notified_at: Optional[str] = None) -> bool:
    """Update a user's status in the database."""
    if not KAPSO_API_KEY:
        logging.info(f"[Mock] Update user {user_id} status={status}")
        return True
    
    url = f"{KAPSO_BASE_URL}/platform/v1/db/users"
    params = {"id": f"eq.{user_id}"}
    payload = {"status": status}
    if notified_at:
        payload["notified_at"] = notified_at
    try:
        r = requests.patch(url, params=params, json=payload, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"Error updating user {user_id}: {e}")
        return False

