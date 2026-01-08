#!/usr/bin/env python3
import os
import re
import json
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Set

import logging
import requests
from dotenv import load_dotenv

load_dotenv()

from kapso_notifier import (  # noqa: E402
    send_template_message,
    send_whatsapp_message,
    get_active_users,
    get_pending_users_for_followup,
    update_user_status,
)

BASE_API = os.getenv("SALTALA_BASE", "https://saltala.apisaltala.com/api/v1")
PUBLIC_URL = os.getenv("PUBLIC_URL", "lobarnechea")

TARGET_LINE_NAMES_RAW = os.getenv("TARGET_LINE_NAMES", "Renovación")
TARGET_LINE_NAMES = [s.strip() for s in TARGET_LINE_NAMES_RAW.split(",") if s.strip()]
# Pistas / fallback
FALLBACK_LINE_ID = int(os.getenv("FALLBACK_LINE_ID", "1768"))
UNIT_HINT = os.getenv("UNIT_HINT", "277")
UNIT_HINT = int(UNIT_HINT) if UNIT_HINT and UNIT_HINT.isdigit() else None

# Meses a considerar al pedir días; suele bastar 1–2
NUMBER_OF_MONTH = int(os.getenv("NUMBER_OF_MONTH", "2"))

CORPORATION_ID = int(os.getenv("CORPORATION_ID", "0"))

TIMEOUT = (10, 20)  # (connect, read)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
TZ_NAME = os.getenv("TZ_NAME", "America/Santiago")  # used to compute correct offset per date (DST-safe)
TZ_OFFSET = os.getenv("TZ_OFFSET", "")  # optional override like "-03:00" (takes precedence over TZ_NAME)

# Set up logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

# Extra debug: set DEBUG_LOG_PAYLOADS=1 to log sample API payloads
DEBUG_LOG_PAYLOADS = os.getenv("DEBUG_LOG_PAYLOADS", "0") not in ("", "0", "false", "False")

# --- Mocking helpers (for local testing) ---
def _env_list(key: str) -> List[str]:
    raw = os.getenv(key, "")
    # Split on commas or whitespace
    return [s.strip() for s in re.split(r"[,\s]+", raw) if s.strip()]

MOCK_LINE_ID_RAW = os.getenv("MOCK_LINE_ID", "")
MOCK_LINE_ID = int(MOCK_LINE_ID_RAW) if MOCK_LINE_ID_RAW.isdigit() else None
MOCK_LINE_NAME = os.getenv("MOCK_LINE_NAME", "")
MOCK_DAYS = _env_list("MOCK_DAYS")
MOCK_TIMES = _env_list("MOCK_TIMES")

def _headers() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": UA,
        "Origin": f"https://{PUBLIC_URL}.saltala.com",
        "Referer": f"https://{PUBLIC_URL}.saltala.com/",
    }

def _post(path: str, params: Optional[Dict[str, Any]] = None, json_data: Optional[Dict[str, Any]] = None, form_payload: Optional[Dict[str, str]] = None) -> Any:
    url = f"{BASE_API.rstrip('/')}/{path.lstrip('/')}"
    
    # To send multipart/form-data with fields but no files, use `files` param in requests
    files = {k: (None, v) for k, v in form_payload.items()} if form_payload else None

    r = requests.post(url, params=params or {}, json=json_data, files=files, headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        log_data = json_data if json_data else form_payload
        logging.error(f"API error {r.status_code} for {r.url} with data {log_data}: {r.text[:500]}")
        raise requests.HTTPError(f"{r.status_code} Error: {r.text}", response=r)
    try:
        js = r.json()
    except Exception:
        return r.text

    # Los endpoints de Saltala suelen venir como {"success": true, "data": ...}
    if isinstance(js, dict) and "data" in js and isinstance(js.get("success", True), (bool, int)):
        return js["data"]
    return js

def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{BASE_API.rstrip('/')}/{path.lstrip('/')}"
    r = requests.get(url, params=params or {}, headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        # Common "no availability" responses come back as 404 with a short message.
        # Don't spam ERROR logs for that expected case.
        body_lower = (r.text or "").lower()
        if r.status_code == 404 and "no se encontraron horas disponibles" in body_lower:
            logging.info(f"No hay horas disponibles (404) para {r.url}")
        else:
            logging.error(f"API error {r.status_code} for {r.url}: {r.text[:500]}")
        raise requests.HTTPError(f"{r.status_code} Error: {r.text}", response=r)
    try:
        js = r.json()
    except Exception:
        return r.text

    # Los endpoints de Saltala suelen venir como {"success": true, "data": ...}
    if isinstance(js, dict) and "data" in js and isinstance(js.get("success", True), (bool, int)):
        return js["data"]
    return js

def _slug(s: str) -> str:
    s = s.casefold()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s).strip()
    return s

TARGET_SLUGS: Set[str] = {_slug(n) for n in TARGET_LINE_NAMES}

def _matches_target(name: str) -> bool:
    return _slug(name) in TARGET_SLUGS

def discover_corporation_id(public_url: str) -> Optional[int]:
    data = _get("/admin/corporation", {"publicUrl": public_url})
    if isinstance(data, dict):
        for k in ("id", "corporationId"):
            if k in data and isinstance(data[k], int):
                return data[k]
    return None

def extract_unit_ids_from_services(services_payload: Any) -> Set[int]:
    """
    Los esquemas pueden variar; acá rastrillamos posibles campos:
    - lista de servicios con 'unitId', 'scheduleUnitId'
    - o colecciones 'units'/'scheduleUnits' con objetos que tengan 'id'
    """
    unit_ids: Set[int] = set()

    def add_if_int(x):
        if isinstance(x, int):
            unit_ids.add(x)

    def scan(obj: Any):
        if isinstance(obj, dict):
            # claves directas
            for key in ("unitId", "scheduleUnitId", "schedule_unit_id"):
                if key in obj:
                    add_if_int(obj[key])
            # listas anidadas
            for key in ("units", "scheduleUnits", "schedules", "items", "children"):
                if key in obj and isinstance(obj[key], list):
                    for it in obj[key]:
                        scan(it)
        elif isinstance(obj, list):
            for it in obj:
                scan(it)

    scan(services_payload)
    return unit_ids

def list_lines(unit_id: int) -> List[Dict[str, Any]]:
    payload = _get("/schedule/public/lines", {"unitId": unit_id, "isPublic": True})
    # Normalizamos a lista de dicts con al menos 'id' y 'name'
    lines: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for it in payload:
            if isinstance(it, dict) and "id" in it and "name" in it:
                lines.append({"id": int(it["id"]), "name": str(it["name"])})
    elif isinstance(payload, dict):
        # a veces devuelven { items: [...] }
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for it in items:
            if isinstance(it, dict) and "id" in it and "name" in it:
                lines.append({"id": int(it["id"]), "name": str(it["name"])})
    return lines

def get_line_details(line_id: int) -> Dict[str, Any]:
    """Obtiene detalles de una línea por id. Puede incluir scheduleUnitId."""
    try:
        payload = _get(f"/schedule/public/lines/{line_id}")
        if DEBUG_LOG_PAYLOADS:
            logging.info(
                f"Detalles línea sample (lineId={line_id}): {str(payload)[:200]}"
            )
        if isinstance(payload, dict):
            return payload
    except Exception as e:
        logging.error(f"No se pudieron obtener detalles de línea {line_id}: {e}")
    return {}

def discover_line_ids_for_targets() -> Dict[str, int]:
    """
    Devuelve {nombre_encontrado -> lineId} para los TARGET_LINE_NAMES.
    Intenta con UNIT_HINT primero; si no, recorre services -> units -> lines.
    """
    found: Dict[str, int] = {}

    # Mock: devolver un único lineId si fue configurado
    if MOCK_LINE_ID is not None:
        mock_name = MOCK_LINE_NAME or (TARGET_LINE_NAMES[0] if TARGET_LINE_NAMES else "Mock")
        return {mock_name: MOCK_LINE_ID}

    # 1) Si tenemos pista de unit, probamos rápido
    if UNIT_HINT:
        try:
            for ln in list_lines(UNIT_HINT):
                if _matches_target(ln["name"]):
                    found[ln["name"]] = ln["id"]
        except Exception:
            pass
        if len(found) >= len(TARGET_SLUGS):
            return found

    # 2) Descubrimiento completo
    corp_id = CORPORATION_ID

    services = _get("/schedule/public/services", {"corporationId": corp_id})
    unit_ids = extract_unit_ids_from_services(services)
    # Siempre incluye el hint si existe
    if UNIT_HINT:
        unit_ids.add(UNIT_HINT)

    for uid in unit_ids:
        try:
            for ln in list_lines(uid):
                if _matches_target(ln["name"]) and ln["name"] not in found:
                    found[ln["name"]] = ln["id"]
        except Exception:
            continue

    return found

def parse_available_days(payload: Any) -> List[str]:
    """Adapta a distintos esquemas posibles y devuelve YYYY-MM-DD en str."""
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
    """Devuelve una lista de horarios (HH:MM) a partir de diferentes esquemas."""
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
            for key in ("times", "hours", "availableTimes", "availableHours", "slots", "items", "data"):
                if key in obj:
                    scan(obj[key])
            # objetos con campos de hora
            for key in ("hour", "time", "startTime", "start", "hora", "from"):
                if key in obj:
                    add_time_like(obj[key])
        else:
            add_time_like(obj)

    scan(payload)
    return sorted(set(times))

def _normalize_patient_rut(value: str) -> str:
    """
    Devuelve una versión solo-dígitos del RUT. Para evitar asumir reglas del dígito verificador,
    no removemos el último dígito automáticamente.
    """
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return digits

def _format_offset(td) -> str:
    if td is None:
        return "-00:00"
    total = int(td.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh = total // 3600
    mm = (total % 3600) // 60
    return f"{sign}{hh:02d}:{mm:02d}"

def _offset_for_date(date_str: str) -> str:
    """
    Returns an ISO offset like -03:00 for the given YYYY-MM-DD.
    - If TZ_OFFSET is set, uses it.
    - Else computes from TZ_NAME (DST-safe).
    """
    if TZ_OFFSET:
        return TZ_OFFSET
    try:
        tz = ZoneInfo(TZ_NAME)
        d = datetime.strptime(date_str, "%Y-%m-%d")
        # local midnight; offset at that local time
        local = d.replace(tzinfo=tz)
        return _format_offset(local.utcoffset())
    except Exception:
        # safe fallback
        return "-03:00"

def get_available_days(line_id: int, months: int = NUMBER_OF_MONTH, patient_rut: str = "") -> List[str]:
    # Mock: devolver días configurados
    if MOCK_DAYS:
        return sorted(set(MOCK_DAYS))

    params: Dict[str, Any] = {"lineId": line_id, "numberOfMonth": months}
    if patient_rut:
        params["patientRut"] = patient_rut
    payload = _get("/schedule/public/getAvailableReservationDays", params)
    days = parse_available_days(payload)
    if DEBUG_LOG_PAYLOADS:
        logging.info(
            f"Payload días sample (lineId={line_id}): "
            f"{str(payload)[:200]} -> {days[:5]}{'…' if len(days) > 5 else ''}"
        )
    return days

def get_available_times(line_id: int, date: str, patient_rut: str = "") -> List[str]:
    """Intenta obtener horarios disponibles para una línea y fecha."""
    # Mock: devolver horarios configurados
    if MOCK_TIMES:
        return sorted(set(MOCK_TIMES))


    offset = _offset_for_date(date)
    start_time = f"{date}T00:00:00{offset}"
    end_time = f"{date}T23:59:59{offset}"
    params: Dict[str, Any] = {"lineId": line_id, "startTime": start_time, "endTime": end_time}

    if patient_rut:
        params["patientRut"] = patient_rut

    try:
        payload = _get("/schedule/public/reservations", params=params)
        times = parse_available_times(payload)
        if DEBUG_LOG_PAYLOADS:
            logging.info(f"Payload horas (GET /reservations): {str(payload)[:200]} -> {len(times)} times")
        return times
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            # treat as no slots / not found
            return []
        logging.error(f"Error obteniendo horarios via /reservations: {e}")
        return []


def book_appointment(line_id: int, date: str, time: str, user_rut: str, user_first_name: str, user_last_name: str, user_email: Optional[str] = None, user_phone: Optional[str] = None) -> bool:
    """Intenta reservar una hora. Devuelve True si fue exitoso."""
    if not user_rut or not user_first_name or not user_last_name:
        logging.warning("Faltan datos de usuario (RUT, nombre, apellido), no se puede reservar.")
        return False

    full_datetime_str = f"{date}T{time}:00"
    block_payload = {"lineId": line_id, "date": full_datetime_str}

    # Step 1: Add a temporary reservation block
    try:
        logging.info(f"Bloqueando temporalmente el horario {date} {time}...")
        _post("/schedule/public/addReservationTemporalBlock", json_data=block_payload)
        logging.info("Bloqueo temporal exitoso.")
    except Exception as e:
        logging.error(f"No se pudo bloquear el horario: {e}")
        return False

    # Step 2: Generate the reservation with user details
    try:
        logging.info("Enviando datos para generar la reserva...")
        
        fields = [
            {"fieldId": "rut", "value": user_rut},
            {"fieldId": "nombres", "value": user_first_name},
            {"fieldId": "apellidos", "value": user_last_name},
        ]
        if user_email:
            fields.append({"fieldId": "correo", "value": user_email})
        if user_phone:
            fields.append({"fieldId": "telefono", "value": user_phone})

        reservation_payload = {
            "lineId": line_id,
            "date": full_datetime_str,
            "fields": fields
        }
        
        form_data = {'payload': json.dumps(reservation_payload)}
        
        result = _post("/schedule/public/generateReservation", form_payload=form_data)
        logging.info(f"Reserva generada exitosamente! Respuesta: {str(result)[:300]}")
        

        return True
    except Exception as e:
        logging.error(f"Falló el intento de generar la reserva: {e}")
        # Cleanup: remove temporary block
        try:
            logging.info("Intentando liberar el bloqueo temporal...")
            _post("/schedule/public/removeReservationTemporalBlock", json_data=block_payload)
        except Exception as e_remove:
            logging.error(f"No se pudo liberar el bloqueo: {e_remove}")
        return False


def main() -> int:
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"[{started}] Chequeando disponibilidad…")

    # 1) Fetch users from Kapso
    active_users = get_active_users()
    pending_users = get_pending_users_for_followup(hours=1)

    # If there are no registered users at all, do nothing (skip API checks entirely).
    if not active_users and not pending_users:
        logging.info("No hay usuarios registrados en Kapso. No se realiza chequeo.")
        return 0

    autobook_users = [u for u in active_users if u.get("mode") == "autobook"]
    notify_users = [u for u in active_users if u.get("mode") == "notify"]
    
    logging.info(f"Usuarios activos: {len(active_users)} total ({len(autobook_users)} auto-book, {len(notify_users)} notify-only)")

    # 2) Send follow-ups to users pending >1 hour
    for user in pending_users:
        phone = user.get("phone", "")
        if phone:
            msg = "¿Completaste tu reserva manual? Responde SÍ o DONE para desactivar las notificaciones."
            send_whatsapp_message(phone, msg)
            logging.info(f"Follow-up enviado a {phone}")

    # Use any registered user's RUT (digits-only) for endpoints that require patientRut
    patient_rut = ""
    for user in (active_users + pending_users):
        patient_rut = _normalize_patient_rut(str(user.get("rut", "") or ""))
        if patient_rut:
            break

    # 3) Descubrir lineId(s)
    targets = discover_line_ids_for_targets()
    if not targets:
        # fallback a mano si no encontramos nada
        targets = {"Renovación": FALLBACK_LINE_ID}

    logging.info("Líneas objetivo: " + str(targets))

    # 4) Consultar disponibilidad
    for name, lid in targets.items():
        try:
            days = get_available_days(lid, NUMBER_OF_MONTH, patient_rut=patient_rut)
        except Exception as e:
            logging.error(f"Error consultando días para {name} (lineId={lid}): {e}")
            continue

        if not days:
            continue

        # Encontramos días, procesamos el primero
        first_day = days[0]
        logging.info(f"Disponibilidad para {name} el {first_day} (total días: {len(days)})")

        times = get_available_times(lid, first_day, patient_rut=patient_rut)
        reserva_url = f"https://{PUBLIC_URL}.saltala.com/#/fila/{lid}/reserva"
        
        # 5) Attempt auto-booking for first autobook user (FIFO)
        booked_user = None
        if times and autobook_users:
            first_time = times[0]
            first_user = autobook_users[0]  # Already sorted by registered_at
            logging.info(f"Intentando auto-booking para {first_user.get('phone')} a las {first_time}...")
            
            if book_appointment(
                lid, first_day, first_time,
                first_user.get("rut", ""),
                first_user.get("first_name", ""),
                first_user.get("last_name", ""),
                first_user.get("email"),
                first_user.get("phone")
            ):
                booked_user = first_user
                # Deactivate the user who got booked
                update_user_status(first_user.get("id"), "inactive")
                # Notify them of success
                success_msg = (
                    f"✅ ¡Cita agendada exitosamente!\n"
                    f"Día: {first_day}\n"
                    f"Hora: {first_time}\n"
                    f"Reserva: {reserva_url}"
                )
                send_whatsapp_message(first_user.get("phone", ""), success_msg)
                logging.info(f"Auto-booking exitoso para {first_user.get('phone')}")
            else:
                logging.error(f"Auto-booking falló para {first_user.get('phone')}")

        # 6) Notify all active users (including notify-only and remaining autobook users)
        notified_user_ids = []
        for user in active_users:
            # Skip if already booked
            if booked_user and user.get("id") == booked_user.get("id"):
                continue
            
            phone = user.get("phone", "")
            if not phone:
                continue
            
            # Build notification message
            msg = (
                f"🎉 ¡Hay disponibilidad para *{name}*!\n"
                f"Primer día: {first_day}\n"
            )
            if times:
                msg += f"Horarios ({len(times)}): {', '.join(times[:5])}{'…' if len(times) > 5 else ''}\n"
            else:
                msg += "No se pudieron obtener los horarios.\n"
            msg += f"Días ({len(days)}): {', '.join(days[:10])}{'…' if len(days) > 10 else ''}\n"
            msg += f"Reserva: {reserva_url}"
            
            # Try template first, fallback to regular message
            if send_template_message(phone, "slot_available", [first_day, reserva_url]):
                notified_user_ids.append(user.get("id"))
            elif send_whatsapp_message(phone, msg):
                notified_user_ids.append(user.get("id"))
        
        # 7) Update notified users to pending status
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for user_id in notified_user_ids:
            update_user_status(user_id, "pending", notified_at=now_iso)
        
        # If we found availability, exit after processing
        return 0

    logging.info("Sin días disponibles en este momento.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
