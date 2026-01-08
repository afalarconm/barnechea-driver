import os
import logging
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime

KAPSO_API_KEY = os.getenv("KAPSO_API_KEY", "")
KAPSO_PHONE_NUMBER_ID = os.getenv("KAPSO_PHONE_NUMBER_ID", "")
KAPSO_BASE_URL = "https://api.kapso.ai"

TIMEOUT = (10, 20)

def _headers() -> Dict[str, str]:
    return {
        "X-API-Key": KAPSO_API_KEY,
        "Content-Type": "application/json",
    }

def send_whatsapp_message(to_phone: str, text: str) -> bool:
    """Send a text message to a WhatsApp number."""
    if not KAPSO_API_KEY or not KAPSO_PHONE_NUMBER_ID:
        logging.info(f"[Mock] WhatsApp to {to_phone}: {text}")
        return True
    
    url = f"{KAPSO_BASE_URL}/meta/whatsapp/v24.0/{KAPSO_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text}
    }
    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        logging.info(f"WhatsApp sent to {to_phone}")
        return True
    except Exception as e:
        logging.error(f"WhatsApp error to {to_phone}: {e}")
        return False

def send_template_message(to_phone: str, template_name: str, params: List[str]) -> bool:
    """Send a template message (for business-initiated conversations)."""
    if not KAPSO_API_KEY or not KAPSO_PHONE_NUMBER_ID:
        logging.info(f"[Mock] Template {template_name} to {to_phone}: {params}")
        return True
    
    url = f"{KAPSO_BASE_URL}/meta/whatsapp/v24.0/{KAPSO_PHONE_NUMBER_ID}/messages"
    components = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "es"},
            "components": components
        }
    }
    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        logging.info(f"Template {template_name} sent to {to_phone}")
        return True
    except Exception as e:
        logging.error(f"Template error to {to_phone}: {e}")
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
        return r.json().get("data", [])
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

