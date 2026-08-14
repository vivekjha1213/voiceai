"""Small, synchronous Cliniko API client used exclusively by the backend.

The agent never talks to Cliniko directly: that keeps credentials out of the
worker process and makes local idempotency/appointment records authoritative.
"""
from __future__ import annotations

import os
from typing import Any
from functools import lru_cache

import httpx


class ClinikoError(RuntimeError):
    pass


class ClinikoClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("CLINIKO_API_KEY")
        shard = os.getenv("CLINIKO_SHARD", "au1")
        self.base_url = os.getenv("CLINIKO_BASE_URL", f"https://api.{shard}.cliniko.com/v1")
        self.appointment_type_id = os.getenv("CLINIKO_APPOINTMENT_TYPE_ID")
        self.enabled = bool(self.api_key)
        self._http: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if not self.api_key:
            raise ClinikoError("CLINIKO_API_KEY is not configured")
        # Cliniko requires HTTP Basic authentication with the API key as the
        # username, JSON accept header, and a descriptive User-Agent.
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url.rstrip("/"),
                auth=(self.api_key, ""),
                headers={
                    "Accept": "application/json",
                    "User-Agent": os.getenv("CLINIKO_USER_AGENT", "VoiceAI Clinic Receptionist (support@example.com)"),
                },
                timeout=httpx.Timeout(connect=2.0, read=8.0, write=8.0, pool=2.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = response.text[:500]
        raise ClinikoError(f"Cliniko returned {response.status_code}: {detail}")

    def create_patient(self, full_name: str, phone_e164: str) -> dict[str, Any]:
        first_name, _, last_name = full_name.strip().partition(" ")
        if not first_name:
            raise ClinikoError("A new patient needs a name")
        response = self._client().post("/patients", json={
            "first_name": first_name,
            "last_name": last_name or first_name,
            "patient_phone_numbers": [{"number": phone_e164, "phone_type": "Mobile"}],
        })
        self._raise(response)
        return response.json()

    def create_appointment(
        self, *, business_id: str, practitioner_id: str, patient_id: str,
        starts_at: str, ends_at: str, idempotency_key: str,
    ) -> dict[str, Any]:
        if not self.appointment_type_id:
            raise ClinikoError("CLINIKO_APPOINTMENT_TYPE_ID is required when Cliniko is enabled")
        payload = {
            "appointment_type_id": self.appointment_type_id,
            "business_id": business_id,
            "practitioner_id": practitioner_id,
            "patient_id": patient_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            # Cliniko has no documented idempotency header. This makes a later
            # reconciliation search possible without exposing call content.
            "notes": f"VoiceAI booking reference: {idempotency_key}",
        }
        response = self._client().post("/individual_appointments", json=payload)
        self._raise(response)
        return response.json()

    def cancel_appointment(self, appointment_id: str, reason: str | None) -> None:
        response = self._client().patch(
            f"/individual_appointments/{appointment_id}/cancel",
            json={"cancellation_reason": 50, "cancellation_note": reason or "Cancelled via VoiceAI"},
        )
        self._raise(response)

    def reschedule_appointment(self, appointment_id: str, starts_at: str, ends_at: str) -> dict[str, Any]:
        response = self._client().patch(
            f"/individual_appointments/{appointment_id}",
            json={"starts_at": starts_at, "ends_at": ends_at},
        )
        self._raise(response)
        return response.json()


@lru_cache(maxsize=1)
def get_cliniko_client() -> ClinikoClient:
    """One keep-alive connection pool per backend process."""
    return ClinikoClient()
