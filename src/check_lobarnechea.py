#!/usr/bin/env python3
import os
import re
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

TIMEOUT = (10, 20)  # (connect, read)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"

# Set up logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

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

def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{BASE_API.rstrip('/')}/{path.lstrip('/')}"
    r = requests.get(url, params=params or {}, headers=_headers(), timeout=TIMEOUT)
    if r.status_code >= 400:
        logging.error(f"API error {r.status_code} for {url}: {r.text[:500]}")
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

def get_available_days(line_id: int, months: int = NUMBER_OF_MONTH) -> List[str]:
    # Mock: devolver días configurados
    if MOCK_DAYS:
        return sorted(set(MOCK_DAYS))

    payload = _get(
        "/schedule/public/getAvailableReservationDays",
        {"lineId": line_id, "numberOfMonth": months},
    )
    return parse_available_days(payload)

def get_available_times(line_id: int, date: str) -> List[str]:
    """Fetch available reservation times for a specific day."""
    # Mock: devolver horarios configurados
    if MOCK_TIMES:
        return sorted(set(MOCK_TIMES))

    payload = _get("/schedule/public/reservations", {"lineId": line_id, "date": date})
    times = []
    # Assuming payload is a list of dicts with 'hour' or 'time' keys; adjust based on actual response
    if isinstance(payload, list):
        for slot in payload:
            if isinstance(slot, dict) and "hour" in slot:  # or whatever key it uses, e.g., "startTime"
                times.append(str(slot["hour"]))
    return sorted(set(times))  # Deduplicate and sort

def main() -> int:
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"[{started}] Chequeando disponibilidad…")

    # 1) Descubrir lineId(s)
    targets = discover_line_ids_for_targets()
    if not targets:
        # fallback a mano si no encontramos nada
        targets = {"Renovación": FALLBACK_LINE_ID}

    logging.info("Líneas objetivo: " + str(targets))

    # 2) Consultar días para cada línea
    any_available = False
    msgs = []
    for name, lid in targets.items():
        try:
            days = get_available_days(lid, NUMBER_OF_MONTH)
        except Exception as e:
            logging.error(f"Error consultando días (lineId={lid}): {e}")
            continue

        if days:
            any_available = True
            first_day = days[0]
            times = get_available_times(lid, first_day)
            reserva_url = f"https://{PUBLIC_URL}.saltala.com/#/fila/{lid}/reserva"
            logging.info(
                f"Disponibilidad encontrada para {name} (lineId={lid}): {first_day}; "
                f"días={len(days)}, horas={len(times)}"
            )
            msg = (
                f"🎉 ¡Hay días con horas para *{name}*!\n"
                f"Primer día: {first_day}\n"
                f"Horarios disponibles ({len(times)}): {', '.join(times[:5])}{'…' if len(times) > 5 else ''}\n"
                f"Días ({len(days)}): {', '.join(days[:10])}{'…' if len(days) > 10 else ''}\n"
                f"Reserva: {reserva_url}"
            )
            msgs.append(msg)

    if any_available:
        # Enviamos un mensaje por línea para mayor claridad
        for m in msgs:
            logging.info("Enviando notificación por Telegram…")
            ok = tg_notify(m, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            if ok:
                logging.info("Notificación exitosa")
            else:
                logging.error("Fallo al enviar notificación")
    else:
        logging.info("Sin días disponibles en este momento.")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
