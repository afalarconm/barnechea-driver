#!/usr/bin/env python3
"""Main entry point for checking availability and booking appointments."""
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from config import (  # noqa: E402
    EXIT_AVAILABILITY_HANDLED,
    FALLBACK_LINE_ID,
    NUMBER_OF_MONTH,
)
from kapso_notifier import (  # noqa: E402
    send_template_message,
    get_active_users,
    get_pending_users_to_reactivate,
    update_user_status,
    _parse_iso_datetime,
)
from discovery import discover_line_ids_for_targets  # noqa: E402
from availability import get_available_days, get_available_times, normalize_patient_rut  # noqa: E402
from booking import autobook_fifo  # noqa: E402

# Set up logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')


def main() -> int:
    """
    Main orchestrator: check availability and handle bookings/notifications.
    
    Returns:
        0 if no availability found, EXIT_AVAILABILITY_HANDLED if availability was handled
    """
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"[{started}] Chequeando disponibilidad…")

    # 1) Fetch users from Kapso
    active_users = get_active_users()
    users_to_reactivate = get_pending_users_to_reactivate(hours=24)

    # Defensive FIFO: ensure users are ordered by registration time (oldest first),
    # even if the upstream API ignores/changes ordering semantics.
    active_users = sorted(
        active_users,
        key=lambda u: (_parse_iso_datetime((u or {}).get("registered_at")), str((u or {}).get("id", ""))),
    )

    # If there are no users to process at all (including no reactivations), do nothing.
    if not active_users and not users_to_reactivate:
        logging.info("No hay usuarios registrados en Kapso. No se realiza chequeo.")
        return 0

    autobook_users = [u for u in active_users if u.get("mode") == "autobook"]
    notify_users = [u for u in active_users if u.get("mode") == "notify"]
    
    logging.info(f"Usuarios activos: {len(active_users)} total ({len(autobook_users)} auto-book, {len(notify_users)} notify-only)")

    # 2) Reactivate users who have been pending for >24 hours (no response to buttons)
    for user in users_to_reactivate:
        user_id = user.get("id")
        if user_id:
            if update_user_status(user_id, "active"):
                # Treat as active for the current run to avoid waiting for the next poll.
                user["status"] = "active"
                active_users.append(user)
                logging.info(f"Usuario {user.get('phone', user_id)} reactivado después de 24hrs sin respuesta")

    # Recompute FIFO ordering and mode splits after possible reactivations.
    active_users = sorted(
        active_users,
        key=lambda u: (_parse_iso_datetime((u or {}).get("registered_at")), str((u or {}).get("id", ""))),
    )
    autobook_users = [u for u in active_users if u.get("mode") == "autobook"]
    notify_users = [u for u in active_users if u.get("mode") == "notify"]

    # Use any registered user's RUT (digits-only) for endpoints that require patientRut
    patient_rut = ""
    for user in active_users:
        patient_rut = normalize_patient_rut(str(user.get("rut", "") or ""))
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
        
        # 5) Attempt auto-booking for ALL autobook users (FIFO), consuming slots as we succeed.
        booked_users = autobook_fifo(
            line_id=lid,
            day=first_day,
            times=times,
            autobook_users=autobook_users,
        )
        booked_user_ids = {u.get("id") for u in booked_users if u.get("id")}

        # 6) Notify all non-booked active users with interactive template (buttons)
        notified_user_ids = []
        button_payloads = ["booked", "not_booked"]  # Payloads for "Ya reserve" / "No pude" buttons
        
        for user in active_users:
            # Skip if already booked
            if user.get("id") in booked_user_ids:
                continue
            
            phone = user.get("phone", "")
            if not phone:
                continue
            
            # Send template with Quick Reply buttons
            if send_template_message(phone, "slot_available_v2", [first_day], button_payloads):
                notified_user_ids.append(user.get("id"))
        
        # 7) Update notified users to pending status
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for user_id in notified_user_ids:
            update_user_status(user_id, "pending", notified_at=now_iso)
        
        # If we found availability, exit after processing
        return EXIT_AVAILABILITY_HANDLED

    logging.info("Sin días disponibles en este momento.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
