from __future__ import annotations
from typing import Optional

import os
from fastapi import FastAPI, HTTPException, Depends, Header
from datetime import datetime
from .database import init_db, get_session
from . import crud
from .cliniko import ClinikoError
from sqlmodel import Session
from pydantic import BaseModel
class MetricPayload(BaseModel):
    name: str
    tool: Optional[str] = None
    latency_s: float
    status: int
    payload: Optional[str] = None

app = FastAPI(title="VoiceAI Clinic Backend")


def require_agent_token(x_agent_token: Optional[str] = Header(default=None)):
    expected = os.getenv("AGENT_SERVICE_TOKEN")
    if expected and expected != "changeme" and x_agent_token != expected:
        raise HTTPException(status_code=401, detail="Invalid agent token")


class LookupPayload(BaseModel):
    phone_e164: str


class SearchPayload(BaseModel):
    date_from: str
    date_to: str
    branch_id: Optional[str] = None
    practitioner_id: Optional[str] = None
    specialty: Optional[str] = None
    day_of_week: Optional[list[str]] = None
    time_of_day: Optional[str] = None
    earliest_only: bool = False
    force_refresh: bool = False


class BookPayload(BaseModel):
    idempotency_key: Optional[str] = None
    patient_id: Optional[int] = None
    practitioner_id: int
    branch_id: int
    start_time: str
    end_time: str
    call_session_id: Optional[str] = None
    new_patient: Optional[dict] = None


class ReschedulePayload(BaseModel):
    appointment_id: int
    new_start_time: str
    new_end_time: str


class CancelPayload(BaseModel):
    appointment_id: int
    reason: Optional[str] = None


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/tools/lookup_patient", dependencies=[Depends(require_agent_token)])
def lookup_patient(payload: LookupPayload, session: Session = Depends(get_session)):
    return crud.lookup_patient_by_phone(session, payload.phone_e164)


@app.post("/tools/search_availability", dependencies=[Depends(require_agent_token)])
def search_availability(payload: SearchPayload, session: Session = Depends(get_session)):
    return crud.search_availability(
        session,
        payload.date_from,
        payload.date_to,
        branch_id=payload.branch_id,
        practitioner_id=payload.practitioner_id,
        specialty=payload.specialty,
        day_of_week=payload.day_of_week,
        time_of_day=payload.time_of_day,
        earliest_only=payload.earliest_only,
        force_refresh=payload.force_refresh,
    )


@app.post("/tools/book_appointment", dependencies=[Depends(require_agent_token)])
def book_appointment(payload: BookPayload, session: Session = Depends(get_session)):
    try:
        result = crud.book_appointment(session, payload.dict())
    except ClinikoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if result.get("conflict"):
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/tools/reschedule_appointment", dependencies=[Depends(require_agent_token)])
def reschedule_appointment(payload: ReschedulePayload, session: Session = Depends(get_session)):
    try:
        return crud.reschedule_appointment(session, payload.dict())
    except ClinikoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/tools/cancel_appointment", dependencies=[Depends(require_agent_token)])
def cancel_appointment(payload: CancelPayload, session: Session = Depends(get_session)):
    try:
        return crud.cancel_appointment(session, payload.dict())
    except ClinikoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/calls/start", dependencies=[Depends(require_agent_token)])
def calls_start(payload: dict, session: Session = Depends(get_session)):
    room = payload.get("livekit_room_name")
    phone = payload.get("phone_e164")
    direction = payload.get("direction")
    call_id = crud.create_call_session(session, room, phone, direction)
    return {"call_session_id": call_id}


@app.post("/calls/end", dependencies=[Depends(require_agent_token)])
def calls_end(payload: dict, session: Session = Depends(get_session)):
    call_id = payload.get("call_session_id")
    outcome = payload.get("outcome")
    crud.end_call_session(session, call_id, outcome)
    return {"status": "ok"}


@app.post("/metrics", dependencies=[Depends(require_agent_token)])
def metrics(payload: MetricPayload, session: Session = Depends(get_session)):
    mid = crud.record_metric(session, payload.name, payload.tool, payload.latency_s, payload.status, payload.payload)
    return {"metric_id": mid}
