import os
import logging
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

KAPSO_API_KEY = os.getenv("KAPSO_API_KEY", "")
KAPSO_PHONE_NUMBER_ID = os.getenv("KAPSO_PHONE_NUMBER_ID", "")
KAPSO_BASE_URL = "https://api.kapso.ai"
KAPSO_TEMPLATE_LANGUAGE_CODE = os.getenv("KAPSO_TEMPLATE_LANGUAGE_CODE", "es_AR")
KAPSO_TEMPLATE_PARAM_NAMES = [s.strip() for s in os.getenv("KAPSO_TEMPLATE_PARAM_NAMES", "").split(",") if s.strip()]

TIMEOUT = (10, 20)

def _parse_iso_datetime(value: Any) -> datetime:
    """
    Best-effort ISO8601 parser used for FIFO ordering.
    Unknown / invalid values sort last (datetime.max).
    Accepts strings like "2026-01-12T12:34:56Z".
    """
    if not isinstance(value, str) or not value:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        # Python doesn't accept trailing "Z" in fromisoformat; convert to +00:00.
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)

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

def send_template_message(
    to_phone: str,
    template_name: str,
    params: List[str],
    button_payloads: Optional[List[str]] = None
) -> bool:
    """Send a template message with optional Quick Reply button payloads."""
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
    
    # Add button payloads if provided (for Quick Reply buttons)
    if button_payloads:
        for idx, payload_str in enumerate(button_payloads):
            components.append({
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(idx),
                "parameters": [{"type": "payload", "payload": payload_str}]
            })

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
        payload = r.json()
        users = payload.get("data", [])
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
            if not users:
                logging.warning(
                    "Kapso DB returned 0 active users. "
                    f"status_code={r.status_code} url={getattr(r, 'url', url)!r} keys={list(payload.keys())}"
                )
        return users
    except requests.HTTPError as e:
        resp_text = (e.response.text[:500] if e.response is not None and e.response.text else "")
        logging.error(f"Error fetching active users: {e} {resp_text}")
        return []
    except Exception as e:
        logging.error(f"Error fetching active users: {e}")
        return []

def get_pending_users_to_reactivate(hours: int = 24) -> List[Dict[str, Any]]:
    """Fetch pending users who haven't responded in X hours (to reactivate)."""
    if not KAPSO_API_KEY:
        return []
    
    url = f"{KAPSO_BASE_URL}/platform/v1/db/users"
    params = {"status": "eq.pending", "order": "notified_at.asc"}
    try:
        r = requests.get(url, params=params, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        users = payload.get("data", [])
        if not isinstance(users, list):
            logging.error(
                "Kapso DB response 'data' is not a list for pending users. "
                f"type={type(users).__name__} status_code={r.status_code} url={getattr(r, 'url', url)!r}"
            )
            return []
        # Filter by time in Python - reactivate users pending for more than X hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        result: List[Dict[str, Any]] = []
        for u in users:
            dt = _parse_iso_datetime((u or {}).get("notified_at"))
            if dt != datetime.max.replace(tzinfo=timezone.utc) and dt < cutoff:
                result.append(u)
        return result
    except requests.HTTPError as e:
        resp_text = (e.response.text[:500] if e.response is not None and e.response.text else "")
        logging.error(f"Error fetching pending users: {e} {resp_text}")
        return []
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

