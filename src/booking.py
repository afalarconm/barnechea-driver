"""Appointment booking logic for Saltala API."""
import json
import logging
from typing import Any, Dict, List, Optional

from config import PUBLIC_URL
from saltala_api import post, SaltalaAPIError
from kapso_notifier import send_whatsapp_message, update_user_status


def _user_display(user: Dict[str, Any]) -> str:
    """Small helper for consistent logs."""
    phone = str((user or {}).get("phone") or "")
    uid = str((user or {}).get("id") or "")
    return phone or uid or "<unknown-user>"


def block_slot(line_id: int, date: str, time: str) -> bool:
    """
    Add a temporary reservation block for a slot.
    
    Args:
        line_id: Line ID
        date: Date in YYYY-MM-DD format
        time: Time in HH:MM format
        
    Returns:
        True if block was successful, False otherwise
    """
    full_datetime_str = f"{date}T{time}:00"
    block_payload = {"lineId": line_id, "date": full_datetime_str}
    
    try:
        logging.info(f"Bloqueando temporalmente el horario {date} {time}...")
        post("/schedule/public/addReservationTemporalBlock", json_data=block_payload)
        logging.info("Bloqueo temporal exitoso.")
        return True
    except SaltalaAPIError as e:
        logging.error(f"No se pudo bloquear el horario: {e}")
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
    
    try:
        logging.info("Enviando datos para generar la reserva...")
        result = post("/schedule/public/generateReservation", form_payload=form_data)
        logging.info(f"Reserva generada exitosamente! Respuesta: {str(result)[:300]}")
        return True
    except SaltalaAPIError as e:
        logging.error(f"Falló el intento de generar la reserva: {e}")
        return False


def remove_block(line_id: int, date: str, time: str) -> None:
    """
    Remove a temporary reservation block.
    
    Args:
        line_id: Line ID
        date: Date in YYYY-MM-DD format
        time: Time in HH:MM format
    """
    full_datetime_str = f"{date}T{time}:00"
    block_payload = {"lineId": line_id, "date": full_datetime_str}
    
    try:
        logging.info("Intentando liberar el bloqueo temporal...")
        post("/schedule/public/removeReservationTemporalBlock", json_data=block_payload)
    except SaltalaAPIError as e_remove:
        logging.error(f"No se pudo liberar el bloqueo: {e_remove}")


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
    if not user_rut or not user_first_name or not user_last_name:
        logging.warning("Faltan datos de usuario (RUT, nombre, apellido), no se puede reservar.")
        return False

    # Step 1: Add a temporary reservation block
    if not block_slot(line_id, date, time):
        return False

    # Step 2: Generate the reservation with user details
    success = generate_reservation(
        line_id, date, time,
        user_rut, user_first_name, user_last_name,
        user_email, user_phone
    )
    
    if not success:
        # Cleanup: remove temporary block
        remove_block(line_id, date, time)
    
    return success


def autobook_fifo(
    *,
    line_id: int,
    day: str,
    times: List[str],
    autobook_users: List[Dict[str, Any]],
    reserva_url: str,
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
        reserva_url: Reservation URL for success message
        
    Returns:
        List of users successfully booked (in booking order)
    """
    if not times or not autobook_users:
        return []

    remaining_times = list(times)
    booked: List[Dict[str, Any]] = []

    for user in autobook_users:
        if not remaining_times:
            break

        # Skip users missing required booking data to avoid burning time on slots.
        if not (user.get("rut") and user.get("first_name") and user.get("last_name")):
            logging.warning(f"Auto-book: skipping user missing required fields: {_user_display(user)}")
            continue

        attempts = 0
        for t in list(remaining_times):
            attempts += 1
            logging.info(f"Auto-book: trying {_user_display(user)} -> {day} {t} ({attempts}/{len(remaining_times)})")

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
                continue

            user_id = user.get("id")
            if user_id:
                update_user_status(user_id, "inactive")
            else:
                logging.warning(f"Auto-book: booked user has no id; cannot mark inactive: {_user_display(user)}")

            success_msg = (
                f"✅ ¡Cita agendada exitosamente!\n"
                f"Día: {day}\n"
                f"Hora: {t}\n"
                f"Reserva: {reserva_url}"
            )
            send_whatsapp_message(user.get("phone", ""), success_msg)

            booked.append(user)
            remaining_times.remove(t)  # consume slot so it can't be reused
            logging.info(f"Auto-book: success for {_user_display(user)} at {day} {t}")
            break

        if attempts == len(remaining_times) and remaining_times:
            logging.warning(f"Auto-book: no slot could be booked for {_user_display(user)} (tried {attempts})")

    return booked
