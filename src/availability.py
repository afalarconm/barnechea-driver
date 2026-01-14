"""Availability checking for Saltala API."""
import logging
import re
from typing import Any, List, Optional

from config import (
    NUMBER_OF_MONTH,
    MOCK_DAYS,
    MOCK_TIMES,
    DEBUG_LOG_PAYLOADS,
    offset_for_date,
)
from saltala_api import get, SaltalaAPIError
import requests


def normalize_patient_rut(value: str) -> str:
    """
    Devuelve una versión solo-dígitos del RUT.
    Para evitar asumir reglas del dígito verificador,
    no removemos el último dígito automáticamente.
    """
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return digits


def parse_available_days(payload: Any) -> List[str]:
    """
    Parse available days from API response.
    
    Adapta a distintos esquemas posibles y devuelve YYYY-MM-DD en str.
    
    Args:
        payload: API response payload
        
    Returns:
        Sorted list of date strings in YYYY-MM-DD format
    """
    days: List[str] = []
    original_payload = payload  # keep for logging

    def maybe_date_str(x: Any) -> Optional[str]:
        if isinstance(x, str):
            # Try exact match YYYY-MM-DD
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
                return x
            # Try extracting from ISO datetime like "2026-01-15T10:00:00"
            m = re.match(r"(\d{4}-\d{2}-\d{2})", x)
            if m:
                return m.group(1)
        if isinstance(x, dict):
            # Try various possible key names for date
            for k in ("date", "day", "dayDate", "fecha", "availableDate", "reservationDate"):
                v = x.get(k)
                if isinstance(v, str):
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                        return v
                    m = re.match(r"(\d{4}-\d{2}-\d{2})", v)
                    if m:
                        return m.group(1)
        return None

    # payload puede ser { days: [...] } o { data: [...] } o lista directa
    if isinstance(payload, dict):
        for key in ("days", "availableDays", "dates", "data", "items", "results", "reservations"):
            if key in payload:
                logging.debug(f"[PARSE_DAYS] Found key '{key}' in payload")
                payload = payload[key]
                break

    if isinstance(payload, list):
        for it in payload:
            d = maybe_date_str(it)
            if d:
                days.append(d)
    elif isinstance(payload, str):
        # rarísimo, pero por si acaso viene como "YYYY-MM-DD,YYYY-MM-DD"
        for token in re.split(r"[,\s]+", payload.strip()):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
                days.append(token)
    
    result = sorted(set(days))
    
    if not result and original_payload:
        logging.warning(f"[PARSE_DAYS] Could not parse any days from payload. Type: {type(original_payload).__name__}")
        if isinstance(original_payload, dict):
            logging.warning(f"[PARSE_DAYS] Available keys: {list(original_payload.keys())}")
    
    return result


def parse_available_times(payload: Any) -> List[str]:
    """
    Parse available times from API response.
    
    Devuelve una lista de horarios (HH:MM) a partir de diferentes esquemas.
    
    Args:
        payload: API response payload
        
    Returns:
        Sorted list of time strings in HH:MM format
    """
    times: List[str] = []
    original_payload = payload  # keep for logging
    found_keys: List[str] = []  # track which keys we found times in

    def add_time_like(value: Any, source_key: str = "") -> None:
        if isinstance(value, str):
            # Normalizar HH:MM[:SS] - also handle ISO datetime "2026-01-15T10:00:00"
            m = re.search(r"(?:T|\b)(\d{2}:\d{2})(?::\d{2})?\b", value)
            if m:
                times.append(m.group(1))
                if source_key and source_key not in found_keys:
                    found_keys.append(source_key)

    def scan(obj: Any, depth: int = 0) -> None:
        if depth > 10:  # prevent infinite recursion
            return
        if isinstance(obj, list):
            for it in obj:
                scan(it, depth + 1)
        elif isinstance(obj, dict):
            # Check for time-like fields first
            for key in (
                "hour",
                "time",
                "startTime",
                "start",
                "hora",
                "from",
                "date",  # sometimes full datetime is in 'date' field
                # Saltala payloads sometimes include ISO datetimes here
                "reservationDate",
                "reservation_date",
                "dateTime",
                "datetime",
            ):
                if key in obj:
                    add_time_like(obj[key], key)
            
            # Recurse into known collection keys
            for key in (
                "times",
                "hours",
                "availableTimes",
                "availableHours",
                "slots",
                "items",
                "data",
                "results",
                "reservations",
            ):
                if key in obj:
                    scan(obj[key], depth + 1)
            
            # IMPORTANT: reservationsById is a dict with ID keys -> recurse into VALUES
            if "reservationsById" in obj and isinstance(obj["reservationsById"], dict):
                for reservation in obj["reservationsById"].values():
                    scan(reservation, depth + 1)
        else:
            add_time_like(obj)

    scan(payload)
    result = sorted(set(times))
    
    if result:
        logging.debug(f"[PARSE_TIMES] Found {len(result)} times from keys: {found_keys}")
    elif original_payload:
        logging.warning(f"[PARSE_TIMES] Could not parse any times from payload. Type: {type(original_payload).__name__}")
        if isinstance(original_payload, dict):
            logging.warning(f"[PARSE_TIMES] Available keys: {list(original_payload.keys())}")
        if isinstance(original_payload, list) and len(original_payload) > 0:
            sample = original_payload[0]
            logging.warning(f"[PARSE_TIMES] First item type: {type(sample).__name__}")
            if isinstance(sample, dict):
                logging.warning(f"[PARSE_TIMES] First item keys: {list(sample.keys())}")
    
    return result


def get_available_days(
    line_id: int,
    months: int = NUMBER_OF_MONTH,
    patient_rut: str = ""
) -> List[str]:
    """
    Get available days for a line.
    
    Args:
        line_id: Line ID to check
        months: Number of months to look ahead
        patient_rut: Optional patient RUT for filtering
        
    Returns:
        List of available dates in YYYY-MM-DD format
    """
    # Mock: devolver días configurados
    if MOCK_DAYS:
        logging.info(f"[MOCK] Returning mock days: {MOCK_DAYS}")
        return sorted(set(MOCK_DAYS))

    params: dict = {"lineId": line_id, "numberOfMonth": months}
    if patient_rut:
        params["patientRut"] = patient_rut
    
    logging.info(f"[DAYS] Fetching available days for lineId={line_id}, months={months}, rut={'***' if patient_rut else 'none'}")
    
    try:
        payload = get("/schedule/public/getAvailableReservationDays", params)
        days = parse_available_days(payload)
        
        # Always log the result for debugging
        logging.info(f"[DAYS] Raw payload type={type(payload).__name__}, parsed {len(days)} days: {days[:10]}{'...' if len(days) > 10 else ''}")
        
        if DEBUG_LOG_PAYLOADS:
            logging.info(f"[DAYS] Full payload: {str(payload)[:500]}")
        
        if not days and payload:
            logging.warning(f"[DAYS] Got payload but parsed 0 days. Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'not a dict'}")
        
        return days
    except SaltalaAPIError as e:
        logging.error(f"[DAYS] Error getting available days for line {line_id}: {e}")
        return []


def get_available_times(
    line_id: int,
    date: str,
    patient_rut: str = ""
) -> List[str]:
    """
    Get available times for a line and date.
    
    Args:
        line_id: Line ID to check
        date: Date in YYYY-MM-DD format
        patient_rut: Optional patient RUT for filtering
        
    Returns:
        List of available times in HH:MM format
    """
    # Mock: devolver horarios configurados
    if MOCK_TIMES:
        logging.info(f"[MOCK] Returning mock times: {MOCK_TIMES}")
        return sorted(set(MOCK_TIMES))

    offset = offset_for_date(date)
    start_time = f"{date}T00:00:00{offset}"
    end_time = f"{date}T23:59:59{offset}"
    params: dict = {"lineId": line_id, "startTime": start_time, "endTime": end_time}

    if patient_rut:
        params["patientRut"] = patient_rut

    logging.info(f"[TIMES] Fetching times for lineId={line_id}, date={date}, offset={offset}")

    try:
        payload = get("/schedule/public/reservations", params=params)
        times = parse_available_times(payload)
        
        # Always log the result
        logging.info(f"[TIMES] Raw payload type={type(payload).__name__}, parsed {len(times)} times: {times}")
        
        if DEBUG_LOG_PAYLOADS:
            logging.info(f"[TIMES] Full payload: {str(payload)[:1000]}")
        
        if not times and payload:
            logging.warning(f"[TIMES] Got payload but parsed 0 times. Payload keys: {list(payload.keys()) if isinstance(payload, dict) else 'not a dict'}")
        
        return times
    except SaltalaAPIError as e:
        if isinstance(e.__cause__, requests.HTTPError) and e.__cause__.response is not None:
            if e.__cause__.response.status_code == 404:
                logging.info(f"[TIMES] 404 response - no slots available for {date}")
                return []
        logging.error(f"[TIMES] Error fetching times via /reservations: {e}")
        return []
