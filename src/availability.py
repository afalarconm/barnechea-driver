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

    def maybe_date_str(x: Any) -> Optional[str]:
        if isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
            return x
        if isinstance(x, dict):
            for k in ("date", "day", "dayDate", "fecha"):
                v = x.get(k)
                if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    return v
        return None

    # payload puede ser { days: [...] } o { data: [...] } o lista directa
    if isinstance(payload, dict):
        for key in ("days", "availableDays", "dates", "data", "items"):
            if key in payload:
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
    return sorted(set(days))


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

    def add_time_like(value: Any) -> None:
        if isinstance(value, str):
            # Normalizar HH:MM[:SS]
            m = re.search(r"\b(\d{2}:\d{2})(?::\d{2})?\b", value)
            if m:
                times.append(m.group(1))

    def scan(obj: Any) -> None:
        if isinstance(obj, list):
            for it in obj:
                scan(it)
        elif isinstance(obj, dict):
            # claves típicas para times/slots
            for key in (
                "times",
                "hours",
                "availableTimes",
                "availableHours",
                "slots",
                "items",
                "data",
                # Saltala payloads seen in the wild
                "reservations",
                "reservationsById",
            ):
                if key in obj:
                    scan(obj[key])
            # objetos con campos de hora
            for key in (
                "hour",
                "time",
                "startTime",
                "start",
                "hora",
                "from",
                # Saltala payloads sometimes include ISO datetimes here
                "reservationDate",
                "reservation_date",
            ):
                if key in obj:
                    add_time_like(obj[key])
        else:
            add_time_like(obj)

    scan(payload)
    return sorted(set(times))


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
        return sorted(set(MOCK_DAYS))

    params: dict = {"lineId": line_id, "numberOfMonth": months}
    if patient_rut:
        params["patientRut"] = patient_rut
    
    try:
        payload = get("/schedule/public/getAvailableReservationDays", params)
        days = parse_available_days(payload)
        if DEBUG_LOG_PAYLOADS:
            logging.info(
                f"Payload días sample (lineId={line_id}): "
                f"{str(payload)[:200]} -> {days[:5]}{'…' if len(days) > 5 else ''}"
            )
        return days
    except SaltalaAPIError as e:
        logging.error(f"Error getting available days for line {line_id}: {e}")
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
        return sorted(set(MOCK_TIMES))

    offset = offset_for_date(date)
    start_time = f"{date}T00:00:00{offset}"
    end_time = f"{date}T23:59:59{offset}"
    params: dict = {"lineId": line_id, "startTime": start_time, "endTime": end_time}

    if patient_rut:
        params["patientRut"] = patient_rut

    try:
        payload = get("/schedule/public/reservations", params=params)
        times = parse_available_times(payload)
        if DEBUG_LOG_PAYLOADS:
            logging.info(
                f"Payload horas (GET /reservations): {str(payload)[:200]} -> {len(times)} times"
            )
        return times
    except SaltalaAPIError as e:
        if isinstance(e.__cause__, requests.HTTPError) and e.__cause__.response is not None:
            if e.__cause__.response.status_code == 404:
                # treat as no slots / not found
                return []
        logging.error(f"Error obteniendo horarios via /reservations: {e}")
        return []
