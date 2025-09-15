#!/usr/bin/env python3
import os
import re
import json
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import requests
import logging
from dotenv import load_dotenv
from telegram_notifier import tg_notify

# Load .env for local runs
load_dotenv()

BASE_API = os.getenv("SALTALA_BASE", "https://saltala.apisaltala.com/api/v1")
PUBLIC_URL = os.getenv("PUBLIC_URL", "lobarnechea")

# Objetivo inicial: solo Renovación, pero se puede pasar más de uno (coma-separado)
TARGET_LINE_NAMES_RAW = os.getenv("TARGET_LINE_NAMES", "Renovación")
TARGET_LINE_NAMES = [s.strip() for s in TARGET_LINE_NAMES_RAW.split(",") if s.strip()]

# Pistas / fallback
FALLBACK_LINE_ID = int(os.getenv("FALLBACK_LINE_ID", "1768"))
UNIT_HINT = os.getenv("UNIT_HINT", "277")
UNIT_HINT = int(UNIT_HINT) if UNIT_HINT and UNIT_HINT.isdigit() else None

# Meses a considerar al pedir días; suele bastar 1–2
NUMBER_OF_MONTH = int(os.getenv("NUMBER_OF_MONTH", "2"))

CORPORATION_ID = int(os.getenv("CORPORATION_ID", "0"))

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
USER_RUT = os.getenv("USER_RUT", "")
USER_FIRST_NAME = os.getenv("USER_FIRST_NAME", "")
USER_LAST_NAME = os.getenv("USER_LAST_NAME", "")
USER_EMAIL = os.getenv("USER_EMAIL", "")
USER_PHONE = os.getenv("USER_PHONE", "")

TIMEOUT = (10, 20)  # (connect, read)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"

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

def get_available_days(line_id: int, months: int = NUMBER_OF_MONTH) -> List[str]:
    # Mock: devolver días configurados
    if MOCK_DAYS:
        return sorted(set(MOCK_DAYS))

    payload = _get(
        "/schedule/public/getAvailableReservationDays",
        {"lineId": line_id, "numberOfMonth": months},
    )
    days = parse_available_days(payload)
    if DEBUG_LOG_PAYLOADS:
        logging.info(
            f"Payload días sample (lineId={line_id}): "
            f"{str(payload)[:200]} -> {days[:5]}{'…' if len(days) > 5 else ''}"
        )
    return days

def get_available_times(line_id: int, date: str) -> List[str]:
    """Intenta obtener horarios disponibles para una línea y fecha."""
    # Mock: devolver horarios configurados
    if MOCK_TIMES:
        return sorted(set(MOCK_TIMES))

    attempts_log: List[str] = []
    
    line_details = get_line_details(line_id)
    schedule_unit_id = None
    for k in ("scheduleUnitId", "schedule_unit_id", "unitId", "schedule_unit"):
        v = line_details.get(k) if isinstance(line_details, dict) else None
        if isinstance(v, int):
            schedule_unit_id = v
            break

    # Intento 1: GET a /appointments con scheduleUnitId, que pareció el más prometedor
    if schedule_unit_id:
        try:
            params = {
                "scheduleUnitId": schedule_unit_id,
                "onlyPublic": True,
                "date": date,
                "appointmentStatuses": [1, 3]
            }
            payload = _get("/schedule/public/appointments", params)
            times = parse_available_times(payload)
            if DEBUG_LOG_PAYLOADS:
                logging.info(f"Payload horas (GET /appointments): {str(payload)[:200]} -> {len(times)} times")
            if times:
                return times
            attempts_log.append(f"GET /appointments {params} -> parsed 0")
        except requests.HTTPError as e:
            # Un 404 con "no se encontraron" es un "éxito" (no hay horas), no un error de request
            if e.response and e.response.status_code == 404 and "no se encontraron" in e.response.text.lower():
                 attempts_log.append(f"GET /appointments {params} -> 404 No hay horas")
            else:
                attempts_log.append(f"GET /appointments {params} -> HTTP {e.response.status_code if e.response else '??'}")
        except Exception as e:
            attempts_log.append(f"GET /appointments {params} -> {type(e).__name__}")

    # Intento 2: POST a un endpoint que podría devolver horas
    try:
        payload = _post(
            "/schedule/public/getAvailableReservationTimes",
            json_data={"lineId": line_id, "date": date},
        )
        times = parse_available_times(payload)
        if DEBUG_LOG_PAYLOADS:
            logging.info(f"Payload horas (POST /getAvailable...): {str(payload)[:200]} -> {len(times)} times")
        if times:
            return times
        attempts_log.append(f"POST /getAvailable... with lineId -> parsed 0")
    except Exception:
        attempts_log.append(f"POST /getAvailable... with lineId -> failed")

    if attempts_log:
        logging.warning(
            f"No se pudieron obtener horarios (lineId={line_id}, date={date}). Intentos: {' | '.join(attempts_log)}"
        )
    return []


def book_appointment(line_id: int, date: str, time: str) -> bool:
    """Intenta reservar una hora. Devuelve True si fue exitoso."""
    if not USER_RUT or not USER_FIRST_NAME or not USER_LAST_NAME:
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
            {"fieldId": "rut", "value": USER_RUT},
            {"fieldId": "nombres", "value": USER_FIRST_NAME},
            {"fieldId": "apellidos", "value": USER_LAST_NAME},
        ]
        if USER_EMAIL:
            fields.append({"fieldId": "correo", "value": USER_EMAIL})
        if USER_PHONE:
            fields.append({"fieldId": "telefono", "value": USER_PHONE})

        reservation_payload = {
            "lineId": line_id,
            "date": full_datetime_str,
            "fields": fields
        }
        
        form_data = {'payload': json.dumps(reservation_payload)}
        
        result = _post("/schedule/public/generateReservation", form_payload=form_data)
        logging.info(f"Reserva generada exitosamente! Respuesta: {str(result)[:300]}")
        
        # Notificar éxito de reserva
        msg = (
            f"✅ ¡Cita para Renovación agendada!\n"
            f"Día: {date}\n"
            f"Hora: {time}\n"
            f"RUT: {USER_RUT}\n"
            f"Nombre: {USER_FIRST_NAME} {USER_LAST_NAME}"
        )
        if USER_EMAIL:
            msg += f"\nEmail: {USER_EMAIL}"
        if USER_PHONE:
            msg += f"\nTeléfono: {USER_PHONE}"

        tg_notify(msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
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

    # 1) Descubrir lineId(s)
    targets = discover_line_ids_for_targets()
    if not targets:
        # fallback a mano si no encontramos nada
        targets = {"Renovación": FALLBACK_LINE_ID}

    logging.info("Líneas objetivo: " + str(targets))

    # 2) Consultar y reservar
    for name, lid in targets.items():
        try:
            days = get_available_days(lid, NUMBER_OF_MONTH)
        except Exception as e:
            logging.error(f"Error consultando días para {name} (lineId={lid}): {e}")
            continue

        if not days:
            continue

        # Encontramos días, procesamos el primero y salimos.
        first_day = days[0]
        logging.info(f"Disponibilidad para {name} el {first_day} (total días: {len(days)})")

        times = get_available_times(lid, first_day)
        if times:
            first_time = times[0]
            logging.info(f"Horarios disponibles: {times}. Intentando reservar a las {first_time}.")
            
            if book_appointment(lid, first_day, first_time):
                logging.info(f"Reserva para {name} completada.")
                return 0  # Éxito
            else:
                logging.error(f"Falló la reserva para {name}. Se notificará la disponibilidad.")
        else:
            logging.warning(f"No se pudieron obtener horarios para {name} el {first_day}.")

        # Notificar disponibilidad (si la reserva falló o no había horas)
        reserva_url = f"https://{PUBLIC_URL}.saltala.com/#/fila/{lid}/reserva"
        msg = (
            f"🎉 ¡Hay disponibilidad para *{name}*!\n"
            f"Primer día: {first_day}\n"
        )
        if times:
            msg += f"Horarios ({len(times)}): {', '.join(times[:5])}{'…' if len(times) > 5 else ''}\n"
        else:
            msg += "No se pudieron obtener los horarios.\n"
        msg += f"Días ({len(days)}): {', '.join(days[:10])}{'…' if len(days) > 10 else ''}\n"
        msg += f"Reserva manual: {reserva_url}"
        
        tg_notify(msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        
        # Salimos después de encontrar la primera disponibilidad
        return 0

    logging.info("Sin días disponibles en este momento.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
