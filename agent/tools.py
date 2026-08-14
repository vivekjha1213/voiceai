import os
import uuid
import httpx
import re
from typing import Any
import dateparser
from livekit.agents import function_tool

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
AGENT_TOKEN = os.environ.get("AGENT_SERVICE_TOKEN", "changeme")

_client = httpx.AsyncClient(base_url=BACKEND_URL, headers={"X-Agent-Token": AGENT_TOKEN}, timeout=8.0)


async def record_metric(name: str, tool: str | None, latency_s: float, status: int, payload: str | None = None):
    try:
        await _client.post("/metrics", json={"name": name, "tool": tool, "latency_s": latency_s, "status": status, "payload": payload})
    except Exception:
        pass


def new_idempotency_key(intent: str, call_session_id: str) -> str:
    return f"{intent}:{call_session_id}:{uuid.uuid4().hex[:8]}"


@function_tool
async def lookup_patient(phone_e164: str) -> dict:
    """Look up the caller by phone number. Returns whether they are a returning
    patient, their recent appointments, and whether this call should be treated
    as a callback to a missed outbound call or a resume of a dropped call."""
    import time
    start = time.time()
    r = await _client.post("/tools/lookup_patient", json={"phone_e164": phone_e164})
    elapsed = time.time() - start
    status = getattr(r, "status_code", 500)
    await record_metric("lookup_patient", "lookup_patient", elapsed, status, payload=str({"phone_e164": phone_e164}))
    r.raise_for_status()
    return r.json()


@function_tool
async def search_availability(
    date_from: str,
    date_to: str,
    branch_id: str | None = None,
    practitioner_id: str | None = None,
    specialty: str | None = None,
    day_of_week: list[str] | None = None,
    time_of_day: str | None = None,
    earliest_only: bool = False,
    force_refresh: bool = False,
) -> dict:
    """Search live appointment availability. Leave branch_id/practitioner_id unset
    to search across ALL branches and practitioners (required for earliest-slot
    and specialty-only queries). Set force_refresh=true whenever confirming a slot
    right before booking, or when the caller changed the requested time."""
    payload = {
        "date_from": date_from, "date_to": date_to, "branch_id": branch_id,
        "practitioner_id": practitioner_id, "specialty": specialty,
        "day_of_week": day_of_week, "time_of_day": time_of_day,
        "earliest_only": earliest_only, "force_refresh": force_refresh,
    }
    import time
    elapsed = 0
    try:
        start = time.time()
        r = await _client.post("/tools/search_availability", json=payload)
        elapsed = time.time() - start
        status = getattr(r, "status_code", 500)
        await record_metric("search_availability", "search_availability", elapsed, status, payload=str({"date_from": date_from, "date_to": date_to}))
        r.raise_for_status()
        return r.json()
    except Exception:
        await record_metric("search_availability", "search_availability", elapsed, 500, payload=str(payload))
        raise


@function_tool
async def book_appointment(
    call_session_id: str,
    practitioner_id: str,
    branch_id: str,
    start_time: str,
    end_time: str,
    patient_id: str | None = None,
    new_patient_phone: str | None = None,
    new_patient_name: str | None = None,
) -> dict:
    """Confirm a booking. Call search_availability(force_refresh=true) for this
    exact slot immediately before calling this. Provide patient_id for a known
    patient, or new_patient_phone/new_patient_name for a first-time caller."""
    payload = {
        "idempotency_key": new_idempotency_key("book", call_session_id),
        "patient_id": patient_id,
        "practitioner_id": practitioner_id,
        "branch_id": branch_id,
        "start_time": start_time,
        "end_time": end_time,
        "call_session_id": call_session_id,
    }
    if new_patient_phone and new_patient_name:
        payload["new_patient"] = {"phone_e164": new_patient_phone, "full_name": new_patient_name}
    import time
    start = time.time()
    r = await _client.post("/tools/book_appointment", json=payload)
    elapsed = time.time() - start
    status = getattr(r, "status_code", 500)
    await record_metric("book_appointment", "book_appointment", elapsed, status, payload=str({"practitioner_id": practitioner_id, "start_time": start_time}))
    r.raise_for_status()
    return r.json()


@function_tool
async def reschedule_appointment(
    call_session_id: str, appointment_id: str, new_start_time: str, new_end_time: str,
) -> dict:
    """Reschedule an existing appointment to a new time. Re-check availability for
    the new slot with force_refresh=true first."""
    payload = {
        "idempotency_key": new_idempotency_key("reschedule", call_session_id),
        "appointment_id": appointment_id,
        "new_start_time": new_start_time,
        "new_end_time": new_end_time,
    }
    import time
    start = time.time()
    r = await _client.post("/tools/reschedule_appointment", json=payload)
    elapsed = time.time() - start
    status = getattr(r, "status_code", 500)
    await record_metric("reschedule_appointment", "reschedule_appointment", elapsed, status, payload=str({"appointment_id": appointment_id}))
    r.raise_for_status()
    return r.json()


@function_tool
async def cancel_appointment(call_session_id: str, appointment_id: str, reason: str | None = None) -> dict:
    """Cancel an existing appointment."""
    payload = {
        "idempotency_key": new_idempotency_key("cancel", call_session_id),
        "appointment_id": appointment_id,
        "reason": reason,
    }
    import time
    start = time.time()
    r = await _client.post("/tools/cancel_appointment", json=payload)
    elapsed = time.time() - start
    status = getattr(r, "status_code", 500)
    await record_metric("cancel_appointment", "cancel_appointment", elapsed, status, payload=str({"appointment_id": appointment_id}))
    r.raise_for_status()
    return r.json()


ALL_TOOLS = [lookup_patient, search_availability, book_appointment, reschedule_appointment, cancel_appointment]


@function_tool
async def parse_time_reference(natural_text: str, window_days: int = 28) -> dict:
    """Parse a natural-language time reference into structured availability search parameters.

    Returns a dict with keys compatible with `search_availability`:
    - date_from, date_to (ISO date strings)
    - day_of_week (list of names)
    - time_of_day (HH:MM or keywords like 'morning', 'afternoon')

    This function is intentionally conservative: when ambiguous it returns a
    best-effort structured hint (not a final booking) so the agent can ask
    a clarifying question or call `search_availability` over the returned window.
    """
    text = natural_text.lower()

    # detect weekdays
    weekdays = []
    for name in ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]:
        if name in text:
            weekdays.append(name)

    # detect time keywords
    time_of_day = None
    if "morning" in text:
        time_of_day = "morning"
    elif "afternoon" in text:
        time_of_day = "afternoon"
    elif "evening" in text:
        time_of_day = "evening"

    # explicit time like 4:30 or 16:30 or 'around 1'
    time_match = re.search(r"(\d{1,2}(:\d{2})?)(\s*(am|pm))?", text)
    if time_match:
        raw = time_match.group(0)
        dt = dateparser.parse(raw)
        if dt:
            time_of_day = dt.strftime("%H:%M")

    # explicit date like Dec 13, 2026
    dt = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future"})
    date_from = None
    date_to = None
    if dt and any(c.isdigit() for c in text):
        date_from = dt.date().isoformat()
        date_to = date_from

    # fallback window if only weekdays or vague terms present
    if not date_from:
        from datetime import date, timedelta

        today = date.today()
        date_from = today.isoformat()
        date_to = (today + timedelta(days=window_days)).isoformat()

    result: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
    if weekdays:
        result["day_of_week"] = weekdays
    if time_of_day:
        result["time_of_day"] = time_of_day
    return result


ALL_TOOLS.append(parse_time_reference)
