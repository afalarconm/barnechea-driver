"""Appointment booking logic for Saltala API."""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from saltala_api import post, SaltalaAPIError
from kapso_notifier import send_template_message, update_user_status


def _user_display(user: Dict[str, Any]) -> str:
    """Small helper for consistent logs."""
    phone = str((user or {}).get("phone") or "")
    uid = str((user or {}).get("id") or "")
    return phone or uid or "<unknown-user>"


def _normalize_rut(rut: str) -> str:
    """Normalize RUT to digits-only format (remove hyphens, dots, etc.)."""
    if not rut:
        return ""
    return re.sub(r"\D", "", rut)


def block_slot(line_id: int, date: str, time: str, patient_rut: str = "") -> bool:
    """
    Add a temporary reservation block for a slot.
    
    Args:
        line_id: Line ID
        date: Date in YYYY-MM-DD format
        time: Time in HH:MM format
        patient_rut: Patient RUT (will be normalized to digits-only)
        
    Returns:
        True if block was successful, False otherwise
    """
    full_datetime_str = f"{date}T{time}:00"
    normalized_rut = _normalize_rut(patient_rut)
    
    block_payload: Dict[str, Any] = {"lineId": line_id, "date": full_datetime_str}
    if normalized_rut:
        block_payload["patientRut"] = normalized_rut
    
    logging.info(f"[BLOCK] Attempting to block slot: lineId={line_id}, datetime={full_datetime_str}, rut={normalized_rut or 'none'}")
    logging.info(f"[BLOCK] Payload: {block_payload}")
    
    try:
        result = post("/schedule/public/addReservationTemporalBlock", json_data=block_payload)
        logging.info(f"[BLOCK] SUCCESS! Response: {result}")
        return True
    except SaltalaAPIError as e:
        logging.error(f"[BLOCK] FAILED to block slot {date} {time}: {e}")
        return False


def generate_reservation(
    line_id: int,
    date: str,
    time: str,
    user_rut: str,
    user_first_name: str,
    user_last_name: str,
    user_email: Optional[str] = None,
    user_phone: Optional[str] = None,
) -> bool:
    """
    Generate a reservation with user details.
    
    Args:
        line_id: Line ID
        date: Date in YYYY-MM-DD format
        time: Time in HH:MM format
        user_rut: User RUT
        user_first_name: User first name
        user_last_name: User last name
        user_email: Optional user email
        user_phone: Optional user phone
        
    Returns:
        True if reservation was successful, False otherwise
    """
    full_datetime_str = f"{date}T{time}:00"
    
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
    
    logging.info(f"[RESERVE] Attempting reservation: lineId={line_id}, datetime={full_datetime_str}")
    logging.info(f"[RESERVE] User: rut={user_rut}, name={user_first_name} {user_last_name}, email={user_email}, phone={user_phone}")
    logging.info(f"[RESERVE] Full payload: {reservation_payload}")
    
    try:
        result = post("/schedule/public/generateReservation", form_payload=form_data)
        logging.info(f"[RESERVE] SUCCESS! Response: {str(result)[:500]}")
        return True
    except SaltalaAPIError as e:
        logging.error(f"[RESERVE] FAILED to generate reservation: {e}")
        return False


def remove_block(line_id: int, date: str, time: str, patient_rut: str = "") -> None:
    """
    Remove a temporary reservation block.
    
    Args:
        line_id: Line ID
        date: Date in YYYY-MM-DD format
        time: Time in HH:MM format
        patient_rut: Patient RUT (will be normalized to digits-only)
    """
    full_datetime_str = f"{date}T{time}:00"
    normalized_rut = _normalize_rut(patient_rut)
    
    block_payload: Dict[str, Any] = {"lineId": line_id, "date": full_datetime_str}
    if normalized_rut:
        block_payload["patientRut"] = normalized_rut
    
    logging.info(f"[UNBLOCK] Removing temporary block: lineId={line_id}, datetime={full_datetime_str}")
    
    try:
        result = post("/schedule/public/removeReservationTemporalBlock", json_data=block_payload)
        logging.info(f"[UNBLOCK] Block removed successfully: {result}")
    except SaltalaAPIError as e_remove:
        logging.error(f"[UNBLOCK] FAILED to remove block: {e_remove}")


def book_appointment(
    line_id: int,
    date: str,
    time: str,
    user_rut: str,
    user_first_name: str,
    user_last_name: str,
    user_email: Optional[str] = None,
    user_phone: Optional[str] = None
) -> bool:
    """
    Book an appointment (block slot + generate reservation).
    
    Args:
        line_id: Line ID
        date: Date in YYYY-MM-DD format
        time: Time in HH:MM format
        user_rut: User RUT
        user_first_name: User first name
        user_last_name: User last name
        user_email: Optional user email
        user_phone: Optional user phone
        
    Returns:
        True if booking was successful, False otherwise
    """
    logging.info("[BOOK] ========== Starting booking flow ==========")
    logging.info(f"[BOOK] Slot: lineId={line_id}, date={date}, time={time}")
    logging.info(f"[BOOK] User: rut={user_rut}, first={user_first_name}, last={user_last_name}, email={user_email}, phone={user_phone}")
    
    if not user_rut or not user_first_name or not user_last_name:
        logging.warning(f"[BOOK] ABORT: Missing required user data - rut={bool(user_rut)}, first_name={bool(user_first_name)}, last_name={bool(user_last_name)}")
        return False

    # Step 1: Add a temporary reservation block (include RUT for validation)
    logging.info("[BOOK] Step 1/2: Blocking slot...")
    if not block_slot(line_id, date, time, patient_rut=user_rut):
        logging.error("[BOOK] FAILED at Step 1: Could not block slot")
        return False

    # Step 2: Generate the reservation with user details
    logging.info("[BOOK] Step 2/2: Generating reservation...")
    success = generate_reservation(
        line_id, date, time,
        user_rut, user_first_name, user_last_name,
        user_email, user_phone
    )
    
    if not success:
        logging.error("[BOOK] FAILED at Step 2: Could not generate reservation, cleaning up block...")
        # Cleanup: remove temporary block
        remove_block(line_id, date, time, patient_rut=user_rut)
    else:
        logging.info("[BOOK] ========== BOOKING SUCCESSFUL! ==========")
    
    return success


def autobook_fifo(
    *,
    line_id: int,
    day: str,
    times: List[str],
    autobook_users: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Try to book as many autobook users as possible, in FIFO order, consuming available times.
    
    - Oldest user gets the earliest remaining time.
    - If a booking fails, try the next time for the same user.
    - Each time is used at most once.
    
    Args:
        line_id: Line ID
        day: Date in YYYY-MM-DD format
        times: List of available times in HH:MM format
        autobook_users: List of users to book (should be sorted FIFO)
        
    Returns:
        List of users successfully booked (in booking order)
    """
    logging.info("[AUTOBOOK] ============================================")
    logging.info("[AUTOBOOK] Starting FIFO auto-booking")
    logging.info(f"[AUTOBOOK] lineId={line_id}, day={day}")
    logging.info(f"[AUTOBOOK] Available times ({len(times)}): {times}")
    logging.info(f"[AUTOBOOK] Users to process ({len(autobook_users)}): {[_user_display(u) for u in autobook_users]}")
    
    if not times:
        logging.warning("[AUTOBOOK] No times available, nothing to book")
        return []
    
    if not autobook_users:
        logging.warning("[AUTOBOOK] No autobook users, nothing to book")
        return []

    remaining_times = list(times)
    booked: List[Dict[str, Any]] = []

    for idx, user in enumerate(autobook_users):
        logging.info(f"[AUTOBOOK] --- Processing user {idx+1}/{len(autobook_users)}: {_user_display(user)} ---")
        
        if not remaining_times:
            logging.warning("[AUTOBOOK] No more times available, stopping")
            break

        # Log all user fields for debugging
        logging.info(f"[AUTOBOOK] User data: id={user.get('id')}, rut={user.get('rut')}, first_name={user.get('first_name')}, last_name={user.get('last_name')}, email={user.get('email')}, phone={user.get('phone')}, mode={user.get('mode')}")

        # Skip users missing required booking data to avoid burning time on slots.
        if not (user.get("rut") and user.get("first_name") and user.get("last_name")):
            logging.warning(f"[AUTOBOOK] SKIP user {_user_display(user)} - missing required fields: rut={bool(user.get('rut'))}, first_name={bool(user.get('first_name'))}, last_name={bool(user.get('last_name'))}")
            continue

        user_booked = False
        attempts = 0
        initial_times_count = len(remaining_times)
        
        for t in list(remaining_times):
            attempts += 1
            logging.info(f"[AUTOBOOK] Attempt {attempts}/{initial_times_count}: {_user_display(user)} -> {day} {t}")

            ok = book_appointment(
                line_id,
                day,
                t,
                user.get("rut", ""),
                user.get("first_name", ""),
                user.get("last_name", ""),
                user.get("email"),
                user.get("phone"),
            )
            if not ok:
                logging.info(f"[AUTOBOOK] Attempt {attempts} FAILED, trying next time slot...")
                continue

            logging.info(f"[AUTOBOOK] BOOKING SUCCESS for {_user_display(user)} at {day} {t}")
            
            user_id = user.get("id")
            if user_id:
                logging.info(f"[AUTOBOOK] Marking user {user_id} as inactive...")
                update_user_status(user_id, "inactive")
            else:
                logging.warning(f"[AUTOBOOK] User has no id, cannot mark inactive: {_user_display(user)}")

            # Send booking confirmation via template (required for 24h+ window)
            logging.info(f"[AUTOBOOK] Sending confirmation message to {user.get('phone', '')}...")
            send_template_message(user.get("phone", ""), "booking_confirmed", [day, t])

            booked.append(user)
            remaining_times.remove(t)  # consume slot so it can't be reused
            user_booked = True
            break

        if not user_booked:
            logging.warning(f"[AUTOBOOK] Could not book any slot for {_user_display(user)} after {attempts} attempts")

    logging.info("[AUTOBOOK] ============================================")
    logging.info(f"[AUTOBOOK] SUMMARY: {len(booked)}/{len(autobook_users)} users booked, {len(remaining_times)} times remaining")
    logging.info(f"[AUTOBOOK] Booked users: {[_user_display(u) for u in booked]}")
    logging.info("[AUTOBOOK] ============================================")
    
    return booked
