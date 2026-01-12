"""Line and unit discovery for Saltala API."""
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Set

from config import (
    TARGET_LINE_NAMES,
    FALLBACK_LINE_ID,
    UNIT_HINT,
    CORPORATION_ID,
    MOCK_LINE_ID,
    MOCK_LINE_NAME,
)
from saltala_api import get, SaltalaAPIError


TARGET_SLUGS: Set[str] = set()


def _slug(s: str) -> str:
    """Normalize string to slug for comparison."""
    s = s.casefold()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _matches_target(name: str) -> bool:
    """Check if a line name matches any target name."""
    return _slug(name) in TARGET_SLUGS


def _initialize_target_slugs():
    """Initialize target slugs from TARGET_LINE_NAMES."""
    global TARGET_SLUGS
    TARGET_SLUGS = {_slug(n) for n in TARGET_LINE_NAMES}


# Initialize on module load
_initialize_target_slugs()


def discover_corporation_id(public_url: str) -> Optional[int]:
    """
    Discover corporation ID from public URL.
    
    Args:
        public_url: Public URL identifier
        
    Returns:
        Corporation ID if found, None otherwise
    """
    try:
        data = get("/admin/corporation", {"publicUrl": public_url})
        if isinstance(data, dict):
            for k in ("id", "corporationId"):
                if k in data and isinstance(data[k], int):
                    return data[k]
    except SaltalaAPIError:
        pass
    return None


def extract_unit_ids_from_services(services_payload: Any) -> Set[int]:
    """
    Extract unit IDs from services payload.
    
    Los esquemas pueden variar; acá rastrillamos posibles campos:
    - lista de servicios con 'unitId', 'scheduleUnitId'
    - o colecciones 'units'/'scheduleUnits' con objetos que tengan 'id'
    
    Args:
        services_payload: Services API response payload
        
    Returns:
        Set of unit IDs found
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
    """
    List all lines for a given unit.
    
    Args:
        unit_id: Unit ID to query
        
    Returns:
        List of line dictionaries with 'id' and 'name' keys
    """
    try:
        payload = get("/schedule/public/lines", {"unitId": unit_id, "isPublic": True})
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
    except SaltalaAPIError as e:
        logging.error(f"Error listing lines for unit {unit_id}: {e}")
        return []


def discover_line_ids_for_targets() -> Dict[str, int]:
    """
    Discover line IDs for target line names.
    
    Devuelve {nombre_encontrado -> lineId} para los TARGET_LINE_NAMES.
    Intenta con UNIT_HINT primero; si no, recorre services -> units -> lines.
    
    Returns:
        Dictionary mapping line names to line IDs
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

    try:
        services = get("/schedule/public/services", {"corporationId": corp_id})
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
    except SaltalaAPIError as e:
        logging.error(f"Error during full discovery: {e}")

    return found
