"""Low-level HTTP client for Saltala API."""
import json
import logging
from typing import Any, Dict, Optional
import requests

from config import BASE_API, PUBLIC_URL, TIMEOUT, USER_AGENT, DEBUG_LOG_PAYLOADS


class SaltalaAPIError(Exception):
    """Custom exception for Saltala API errors."""
    pass


def _headers() -> Dict[str, str]:
    """Generate HTTP headers for Saltala API requests."""
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "Origin": f"https://{PUBLIC_URL}.saltala.com",
        "Referer": f"https://{PUBLIC_URL}.saltala.com/",
    }


def _unwrap_response(response_data: Any) -> Any:
    """
    Unwrap Saltala API response format.
    Saltala endpoints typically return {"success": true, "data": ...}
    """
    if isinstance(response_data, dict) and "data" in response_data:
        if isinstance(response_data.get("success", True), (bool, int)):
            return response_data["data"]
    return response_data


def get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """
    Perform GET request to Saltala API.
    
    Args:
        path: API endpoint path
        params: Query parameters
        
    Returns:
        Unwrapped response data
        
    Raises:
        SaltalaAPIError: On HTTP errors
    """
    url = f"{BASE_API.rstrip('/')}/{path.lstrip('/')}"
    
    if DEBUG_LOG_PAYLOADS:
        logging.info(f"[API GET] {url} params={params}")
    
    try:
        r = requests.get(url, params=params or {}, headers=_headers(), timeout=TIMEOUT)
    except requests.RequestException as e:
        logging.error(f"[API GET] Request failed for {url}: {e}")
        raise SaltalaAPIError(f"Request failed: {e}") from e
    
    if DEBUG_LOG_PAYLOADS:
        logging.info(f"[API GET] Response status={r.status_code} body={r.text[:1000]}")
    
    if r.status_code >= 400:
        # Common "no availability" responses come back as 404 with a short message.
        # Don't spam ERROR logs for that expected case.
        body_lower = (r.text or "").lower()
        if r.status_code == 404 and "no se encontraron horas disponibles" in body_lower:
            logging.info(f"No hay horas disponibles (404) para {r.url}")
        else:
            logging.error(f"[API GET] Error {r.status_code} for {r.url}: {r.text[:500]}")
        err = SaltalaAPIError(f"{r.status_code} Error: {r.text}")
        err.__cause__ = requests.HTTPError(response=r)
        raise err
    
    try:
        js = r.json()
    except Exception as e:
        if DEBUG_LOG_PAYLOADS:
            logging.warning(f"[API GET] Could not parse JSON, returning text: {e}")
        return r.text
    
    unwrapped = _unwrap_response(js)
    if DEBUG_LOG_PAYLOADS:
        logging.info(f"[API GET] Unwrapped response type={type(unwrapped).__name__}")
    return unwrapped


def post(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[Dict[str, Any]] = None,
    form_payload: Optional[Dict[str, str]] = None
) -> Any:
    """
    Perform POST request to Saltala API.
    
    Args:
        path: API endpoint path
        params: Query parameters
        json_data: JSON payload
        form_payload: Form data payload (multipart/form-data)
        
    Returns:
        Unwrapped response data
        
    Raises:
        SaltalaAPIError: On HTTP errors
    """
    url = f"{BASE_API.rstrip('/')}/{path.lstrip('/')}"
    
    # To send multipart/form-data with fields but no files, use `files` param in requests
    files = {k: (None, v) for k, v in form_payload.items()} if form_payload else None
    
    log_data = json_data if json_data else form_payload
    if DEBUG_LOG_PAYLOADS:
        logging.info(f"[API POST] {url} params={params} data={log_data}")
    
    try:
        r = requests.post(
            url,
            params=params or {},
            json=json_data,
            files=files,
            headers=_headers(),
            timeout=TIMEOUT
        )
    except requests.RequestException as e:
        logging.error(f"[API POST] Request failed for {url}: {e}")
        raise SaltalaAPIError(f"Request failed: {e}") from e
    
    if DEBUG_LOG_PAYLOADS:
        logging.info(f"[API POST] Response status={r.status_code} body={r.text[:1000]}")
    
    if r.status_code >= 400:
        logging.error(f"[API POST] Error {r.status_code} for {r.url} with data {log_data}: {r.text[:500]}")
        err = SaltalaAPIError(f"{r.status_code} Error: {r.text}")
        err.__cause__ = requests.HTTPError(response=r)
        raise err
    
    try:
        js = r.json()
    except Exception as e:
        if DEBUG_LOG_PAYLOADS:
            logging.warning(f"[API POST] Could not parse JSON, returning text: {e}")
        return r.text
    
    unwrapped = _unwrap_response(js)
    if DEBUG_LOG_PAYLOADS:
        logging.info(f"[API POST] Unwrapped response type={type(unwrapped).__name__} success={js.get('success') if isinstance(js, dict) else 'N/A'}")
    return unwrapped
