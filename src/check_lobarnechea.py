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
    PUBLIC_URL,
)
from kapso_notifier import (  # noqa: E402
    send_template_message,
    send_whatsapp_message,
    get_active_users,
    get_pending_users_for_followup,
    update_user_status,
    _parse_iso_datetime,  # Reuse from kapso_notifier instead of duplicating
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
    pending_users = get_pending_users_for_followup(hours=1)

    # Defensive FIFO: ensure users are ordered by registration time (oldest first),
    # even if the upstream API ignores/changes ordering semantics.
    active_users = sorted(
        active_users,
        key=lambda u: (_parse_iso_datetime((u or {}).get("registered_at")), str((u or {}).get("id", ""))),
    )

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
        reserva_url = f"https://{PUBLIC_URL}.saltala.com/#/fila/{lid}/reserva"
        
        # 5) Attempt auto-booking for ALL autobook users (FIFO), consuming slots as we succeed.
        booked_users = autobook_fifo(
            line_id=lid,
            day=first_day,
            times=times,
            autobook_users=autobook_users,
            reserva_url=reserva_url,
        )
        booked_user_ids = {u.get("id") for u in booked_users if u.get("id")}

        # 6) Notify all notify-only users
        notified_user_ids = []
        for user in active_users:
            # Skip if already booked
            if user.get("id") in booked_user_ids:
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
            
            if send_template_message(phone, "slot_available", [first_day]):
                notified_user_ids.append(user.get("id"))
            elif send_whatsapp_message(phone, msg):
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
