import os
import httpx
from typing import Any
from livekit.agents import function_tool

CLINIKO_SHARD = os.getenv("CLINIKO_SHARD", "au1")
CLINIKO_BASE_URL = os.getenv("CLINIKO_BASE_URL", f"https://api.{CLINIKO_SHARD}.cliniko.com/v1")
CLINIKO_API_KEY = os.getenv("CLINIKO_API_KEY")

if CLINIKO_API_KEY:
    _cliniko_client = httpx.AsyncClient(
        base_url=CLINIKO_BASE_URL,
        # Cliniko API keys use HTTP Basic auth, not Bearer tokens.
        auth=(CLINIKO_API_KEY, ""),
        headers={
            "Accept": "application/json",
            "User-Agent": os.getenv("CLINIKO_USER_AGENT", "VoiceAI Clinic Receptionist (support@example.com)"),
        },
        timeout=httpx.Timeout(connect=2.0, read=8.0, write=8.0, pool=2.0),
    )
else:
    _cliniko_client = None


@function_tool
async def cliniko_lookup_patient(phone_e164: str) -> dict[str, Any]:
    """Try to find a patient in Cliniko by phone number. Returns Cliniko's
    JSON response (may be empty list when no patient is found).
    """
    if _cliniko_client is None:
        raise RuntimeError("CLINIKO_API_KEY is not configured in the environment")

    # This helper is intentionally not registered as an agent tool. The
    # backend is the supported integration point, where local/remote IDs and
    # idempotency are reconciled. A practice may customise its patient-phone
    # filter according to its Cliniko data conventions.
    r = await _cliniko_client.get("/patients", params={"q[]": f"phone:~{phone_e164}"})
    r.raise_for_status()
    return r.json()


@function_tool
async def cliniko_list_practitioners() -> dict[str, Any]:
    """Return practitioners (doctors) from Cliniko.
    """
    if _cliniko_client is None:
        raise RuntimeError("CLINIKO_API_KEY is not configured in the environment")
    r = await _cliniko_client.get("/practitioners")
    r.raise_for_status()
    return r.json()


@function_tool
async def cliniko_list_branches() -> dict[str, Any]:
    """Return businesses (Cliniko's name for clinics/branches).
    """
    if _cliniko_client is None:
        raise RuntimeError("CLINIKO_API_KEY is not configured in the environment")
    r = await _cliniko_client.get("/businesses")
    r.raise_for_status()
    return r.json()


@function_tool
async def cliniko_get_availability(
    business_id: str, practitioner_id: str, appointment_type_id: str,
    date_from: str, date_to: str,
) -> dict[str, Any]:
    """Query Cliniko online-booking availability (maximum seven-day window)."""
    if _cliniko_client is None:
        raise RuntimeError("CLINIKO_API_KEY is not configured in the environment")
    params = {"from": date_from, "to": date_to}
    path = (
        f"/businesses/{business_id}/practitioners/{practitioner_id}"
        f"/appointment_types/{appointment_type_id}/available_times"
    )
    r = await _cliniko_client.get(path, params=params)
    r.raise_for_status()
    return r.json()
