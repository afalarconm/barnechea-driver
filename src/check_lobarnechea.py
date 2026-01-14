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
    logging.info(f"")
    logging.info(f"##############################################################")
    logging.info(f"# BARNECHEA DRIVER - Availability Check Started")
    logging.info(f"# Time: {started}")
    logging.info(f"##############################################################")

    # 1) Fetch users from Kapso
    logging.info(f"[USERS] Fetching users from Kapso...")
    active_users = get_active_users()
    users_to_reactivate = get_pending_users_to_reactivate(hours=24)
    
    logging.info(f"[USERS] Fetched {len(active_users)} active users, {len(users_to_reactivate)} users to reactivate")

    # Defensive FIFO: ensure users are ordered by registration time (oldest first),
    # even if the upstream API ignores/changes ordering semantics.
    active_users = sorted(
        active_users,
        key=lambda u: (_parse_iso_datetime((u or {}).get("registered_at")), str((u or {}).get("id", ""))),
    )

    # If there are no users to process at all (including no reactivations), do nothing.
    if not active_users and not users_to_reactivate:
        logging.info("[USERS] No users registered in Kapso. Skipping availability check.")
        return 0

    autobook_users = [u for u in active_users if u.get("mode") == "autobook"]
    notify_users = [u for u in active_users if u.get("mode") == "notify"]
    
    logging.info(f"[USERS] Active users breakdown: {len(active_users)} total")
    logging.info(f"[USERS]   - autobook: {len(autobook_users)}")
    logging.info(f"[USERS]   - notify:   {len(notify_users)}")
    
    # Log each autobook user for transparency
    for idx, u in enumerate(autobook_users):
        logging.info(f"[USERS] Autobook user {idx+1}: phone={u.get('phone')}, rut={u.get('rut')}, name={u.get('first_name')} {u.get('last_name')}, registered={u.get('registered_at')}")

    # 2) Reactivate users who have been pending for >24 hours (no response to buttons)
    if users_to_reactivate:
        logging.info(f"[REACTIVATE] Reactivating {len(users_to_reactivate)} users pending >24hrs...")
    for user in users_to_reactivate:
        user_id = user.get("id")
        if user_id:
            if update_user_status(user_id, "active"):
                # Treat as active for the current run to avoid waiting for the next poll.
                user["status"] = "active"
                active_users.append(user)
                logging.info(f"[REACTIVATE] User {user.get('phone', user_id)} reactivated after 24hrs no response")

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
    
    logging.info(f"[RUT] Using patient RUT for API calls: {'***' + patient_rut[-4:] if patient_rut else 'none'}")

    # 3) Descubrir lineId(s)
    logging.info(f"[DISCOVERY] Discovering line IDs for targets...")
    targets = discover_line_ids_for_targets()
    if not targets:
        # fallback a mano si no encontramos nada
        logging.warning(f"[DISCOVERY] No targets found, using fallback lineId={FALLBACK_LINE_ID}")
        targets = {"Renovación": FALLBACK_LINE_ID}

    logging.info(f"[DISCOVERY] Target lines: {targets}")

    # 4) Consultar disponibilidad
    logging.info(f"[AVAILABILITY] Checking availability for {len(targets)} target line(s)...")
    
    for name, lid in targets.items():
        logging.info(f"[AVAILABILITY] Checking line '{name}' (lineId={lid})...")
        
        try:
            days = get_available_days(lid, NUMBER_OF_MONTH, patient_rut=patient_rut)
        except Exception as e:
            logging.error(f"[AVAILABILITY] Error checking days for '{name}' (lineId={lid}): {e}")
            continue

        if not days:
            logging.info(f"[AVAILABILITY] No days available for '{name}'")
            continue

        # Encontramos días, procesamos el primero
        first_day = days[0]
        logging.info(f"##############################################################")
        logging.info(f"# AVAILABILITY FOUND!")
        logging.info(f"# Line: {name} (lineId={lid})")
        logging.info(f"# Date: {first_day}")
        logging.info(f"# Total days available: {len(days)}")
        logging.info(f"# All days: {days}")
        logging.info(f"##############################################################")

        logging.info(f"[TIMES] Fetching available times for {first_day}...")
        times = get_available_times(lid, first_day, patient_rut=patient_rut)
        
        if not times:
            logging.warning(f"[TIMES] Day {first_day} has no available times! This is unexpected.")
            logging.warning(f"[TIMES] Skipping this day, trying next if available...")
            continue
        
        logging.info(f"[TIMES] Found {len(times)} available time slots: {times}")
        
        # 5) Attempt auto-booking for ALL autobook users (FIFO), consuming slots as we succeed.
        logging.info(f"[AUTOBOOK] Starting auto-booking process...")
        logging.info(f"[AUTOBOOK] {len(autobook_users)} users to auto-book, {len(times)} slots available")
        
        booked_users = autobook_fifo(
            line_id=lid,
            day=first_day,
            times=times,
            autobook_users=autobook_users,
        )
        booked_user_ids = {u.get("id") for u in booked_users if u.get("id")}
        
        logging.info(f"[AUTOBOOK] Auto-booking complete: {len(booked_users)} users successfully booked")

        # 6) Notify all non-booked active users with interactive template (buttons)
        logging.info(f"[NOTIFY] Notifying remaining users about availability...")
        notified_user_ids = []
        button_payloads = ["booked", "not_booked"]  # Payloads for "Ya reserve" / "No pude" buttons
        
        users_to_notify = [u for u in active_users if u.get("id") not in booked_user_ids]
        logging.info(f"[NOTIFY] {len(users_to_notify)} users to notify (excluding {len(booked_user_ids)} already booked)")
        
        for user in users_to_notify:
            phone = user.get("phone", "")
            if not phone:
                logging.warning(f"[NOTIFY] User {user.get('id')} has no phone, skipping")
                continue
            
            # Send template with Quick Reply buttons
            logging.info(f"[NOTIFY] Sending availability notification to {phone}...")
            if send_template_message(phone, "slot_available_v2", [first_day], button_payloads):
                notified_user_ids.append(user.get("id"))
        
        # 7) Update notified users to pending status
        logging.info(f"[NOTIFY] Updating {len(notified_user_ids)} users to 'pending' status...")
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for user_id in notified_user_ids:
            update_user_status(user_id, "pending", notified_at=now_iso)
        
        logging.info(f"##############################################################")
        logging.info(f"# RUN COMPLETE - AVAILABILITY HANDLED")
        logging.info(f"# Booked: {len(booked_users)} users")
        logging.info(f"# Notified: {len(notified_user_ids)} users")
        logging.info(f"##############################################################")
        
        # If we found availability, exit after processing
        return EXIT_AVAILABILITY_HANDLED

    logging.info(f"##############################################################")
    logging.info(f"# NO AVAILABILITY FOUND")
    logging.info(f"##############################################################")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
