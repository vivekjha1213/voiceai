from __future__ import annotations

from sqlmodel import select
from datetime import datetime, timedelta, time
from sqlmodel import Session
from . import models
from typing import Optional
import os
import httpx
from .cliniko import get_cliniko_client, ClinikoError
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


def lookup_patient_by_phone(session: Session, phone_e164: str):
    stmt = select(models.Patient).where(models.Patient.phone_e164 == phone_e164)
    res = session.exec(stmt).first()
    if res:
        # recent appointments (last 5)
        appts_stmt = select(models.Appointment).where(models.Appointment.patient_id == res.id).order_by(models.Appointment.start_time.desc())
        recent_appts = session.exec(appts_stmt).all()[:5]
        recent = [
            {"id": a.id, "start_time": a.start_time.isoformat(), "status": a.status} for a in recent_appts
        ]

        # detect a recent unfinished call session for callback/resume scenarios
        cs_stmt = select(models.CallSession).where(models.CallSession.phone_e164 == phone_e164).order_by(models.CallSession.started_at.desc())
        last_cs = session.exec(cs_stmt).first()
        resumed = None
        if last_cs and last_cs.ended_at is None:
            resumed = {"reason": "callback", "transcript_state": None, "call_session_id": last_cs.call_session_id}

        return {
            "is_returning": True,
            "full_name": res.full_name,
            "patient_id": res.id,
            "recent_appointments": recent,
            "resumed_call": resumed,
        }
    return {"is_returning": False}


def create_call_session(session: Session, livekit_room_name: str, phone_e164: str, direction: str):
    call_id = f"call_{int(datetime.utcnow().timestamp())}_{phone_e164 or 'anon'}"
    cs = models.CallSession(call_session_id=call_id, phone_e164=phone_e164, direction=direction)
    session.add(cs)
    session.commit()
    session.refresh(cs)
    return cs.call_session_id


def record_metric(
    session: Session,
    name: str,
    tool: Optional[str],
    latency_s: float,
    status: int,
    payload: Optional[str] = None,
):
    m = models.Metric(name=name, tool=tool, latency_s=latency_s, status=status, payload=payload)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m.id


def end_call_session(session: Session, call_session_id: str, outcome: str):
    stmt = select(models.CallSession).where(models.CallSession.call_session_id == call_session_id)
    cs = session.exec(stmt).first()
    if cs:
        cs.ended_at = datetime.utcnow()
        cs.outcome = outcome
        session.add(cs)
        session.commit()


def _generate_daily_slots(
    date_from: datetime,
    date_to: datetime,
    open_time: Optional[str] = None,
    close_time: Optional[str] = None,
    tz: Optional[str] = None,
    slot_minutes: int = 30,
):
    # open_time/close_time as "HH:MM" strings in branch local timezone
    slots = []
    # determine local start/end for the given day
    if open_time:
        oh, om = map(int, open_time.split(":"))
    else:
        oh, om = 9, 0
    if close_time:
        ch, cm = map(int, close_time.split(":"))
    else:
        ch, cm = 17, 0

    local_zone = ZoneInfo(tz) if (tz and ZoneInfo) else None

    # create naive datetimes then convert to UTC-aware if zone provided
    local_start = datetime.combine(date_from.date(), time(hour=oh, minute=om))
    local_end = datetime.combine(date_from.date(), time(hour=ch, minute=cm))

    if local_zone:
        local_start = local_start.replace(tzinfo=local_zone).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        local_end = local_end.replace(tzinfo=local_zone).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    current = local_start
    while current < local_end:
        slots.append((current, current + timedelta(minutes=slot_minutes)))
        current += timedelta(minutes=slot_minutes)
    return slots


def search_availability(session: Session, date_from: str, date_to: str, branch_id: Optional[str] = None,
                        practitioner_id: Optional[str] = None, specialty: Optional[str] = None,
                        day_of_week: Optional[list] = None, time_of_day: Optional[str] = None,
                        earliest_only: bool = False, force_refresh: bool = False):
    # naive availability: generate 30-min slots for practitioners matching filters,
    # exclude already-booked appointments.
    from datetime import datetime

    df = datetime.fromisoformat(date_from)
    dt = datetime.fromisoformat(date_to)

    practitioners = session.exec(select(models.Practitioner)).all()
    if practitioner_id:
        practitioners = [p for p in practitioners if str(p.id) == str(practitioner_id)]
    if branch_id:
        practitioners = [p for p in practitioners if str(p.branch_id) == str(branch_id)]
    results = []
    for p in practitioners:
        # get branch hours/timezone if available
        branch = None
        if p.branch_id:
            branch = session.get(models.Branch, p.branch_id)
        open_time = branch.open_time if branch else None
        close_time = branch.close_time if branch else None
        tz = branch.timezone if branch else None

        # for each day in range, generate slots and exclude conflicts
        day = df
        while day.date() <= dt.date():
            slots = _generate_daily_slots(day, day, open_time=open_time, close_time=close_time, tz=tz)
            for s, e in slots:
                # check overlapping appointments
                stmt = select(models.Appointment).where(
                    models.Appointment.practitioner_id == p.id,
                    models.Appointment.start_time < e,
                    models.Appointment.end_time > s,
                    models.Appointment.status == "booked",
                )
                conflict = session.exec(stmt).first()
                if not conflict:
                    results.append({
                        "practitioner_id": p.id,
                        "practitioner_name": p.full_name,
                        "branch_id": p.branch_id,
                        "start_time": s.isoformat(),
                        "end_time": e.isoformat(),
                    })
            from datetime import timedelta
            day = day + timedelta(days=1)
    if earliest_only and results:
        results.sort(key=lambda x: x["start_time"])
        return {"slots": [results[0]]}
    return {"slots": results}


def book_appointment(session: Session, payload: dict):
    # idempotency
    key = payload.get("idempotency_key")
    if key:
        stmt = select(models.IdempotencyKey).where(models.IdempotencyKey.key == key)
        existing = session.exec(stmt).first()
        if existing:
            appt = session.get(models.Appointment, int(existing.result_reference)) if existing.result_reference else None
            return {
                "idempotency_key": key,
                "appointment_id": appt.id if appt else None,
                "status": "booked",
                "idempotent_replay": True,
            }

    # conflict check: any overlapping appointment for the practitioner
    from datetime import datetime
    start = datetime.fromisoformat(payload["start_time"])
    end = datetime.fromisoformat(payload["end_time"])
    stmt = select(models.Appointment).where(
        models.Appointment.practitioner_id == int(payload["practitioner_id"]),
        models.Appointment.start_time < end,
        models.Appointment.end_time > start,
        models.Appointment.status == "booked",
    )
    conflict = session.exec(stmt).first()
    if conflict:
        return {"conflict": True, "message": "Slot already taken"}

    cliniko = get_cliniko_client()
    cliniko_result = None
    patient_id = payload.get("patient_id")
    if cliniko.enabled:
        branch = session.get(models.Branch, int(payload["branch_id"]))
        practitioner = session.get(models.Practitioner, int(payload["practitioner_id"]))
        if not branch or not practitioner:
            raise ClinikoError("The selected branch or practitioner does not exist")
        if not branch.cliniko_business_id or not practitioner.cliniko_practitioner_id:
            raise ClinikoError("Cliniko IDs are missing for the selected branch or practitioner")

        patient = session.get(models.Patient, int(patient_id)) if patient_id else None
        new_patient = payload.get("new_patient")
        if patient is None and new_patient:
            remote_patient = cliniko.create_patient(new_patient["full_name"], new_patient["phone_e164"])
            patient = models.Patient(
                full_name=new_patient["full_name"],
                phone_e164=new_patient["phone_e164"],
                external_id=str(remote_patient["id"]),
            )
            session.add(patient)
            session.flush()
        if not patient or not patient.external_id:
            raise ClinikoError("A Cliniko-linked patient is required to create an appointment")
        patient_id = patient.id
        cliniko_result = cliniko.create_appointment(
            business_id=branch.cliniko_business_id,
            practitioner_id=practitioner.cliniko_practitioner_id,
            patient_id=patient.external_id,
            starts_at=payload["start_time"],
            ends_at=payload["end_time"],
            idempotency_key=key or f"local-{payload.get('call_session_id', 'unknown')}",
        )

    appt = models.Appointment(
        practitioner_id=int(payload["practitioner_id"]),
        branch_id=int(payload["branch_id"]),
        patient_id=patient_id,
        start_time=start,
        end_time=end,
        call_session_id=payload.get("call_session_id"),
        idempotency_key=key,
        cliniko_appointment_id=str(cliniko_result["id"]) if cliniko_result else None,
    )
    session.add(appt)
    session.flush()
    # record idempotency
    if key:
        ik = models.IdempotencyKey(key=key, result_reference=str(appt.id))
        session.add(ik)
    session.commit()
    session.refresh(appt)
    return {
        "appointment_id": appt.id,
        "status": "booked",
        "cliniko_appointment_id": appt.cliniko_appointment_id,
    }


def reschedule_appointment(session: Session, payload: dict):
    appt_id = int(payload["appointment_id"])
    stmt = select(models.Appointment).where(models.Appointment.id == appt_id)
    appt = session.exec(stmt).first()
    if not appt:
        return {"error": "not_found"}
    from datetime import datetime
    new_start = datetime.fromisoformat(payload["new_start_time"])
    new_end = datetime.fromisoformat(payload["new_end_time"])
    # conflict check
    stmt = select(models.Appointment).where(
        models.Appointment.practitioner_id == appt.practitioner_id,
        models.Appointment.start_time < new_end,
        models.Appointment.end_time > new_start,
        models.Appointment.id != appt.id,
        models.Appointment.status == "booked",
    )
    conflict = session.exec(stmt).first()
    if conflict:
        return {"conflict": True}
    cliniko = get_cliniko_client()
    if cliniko.enabled:
        if not appt.cliniko_appointment_id:
            raise ClinikoError("Local appointment is not linked to a Cliniko appointment")
        cliniko.reschedule_appointment(appt.cliniko_appointment_id, payload["new_start_time"], payload["new_end_time"])
    appt.start_time = new_start
    appt.end_time = new_end
    appt.status = "rescheduled"
    session.add(appt)
    session.commit()
    return {"appointment_id": appt.id, "status": "rescheduled"}


def cancel_appointment(session: Session, payload: dict):
    appt_id = int(payload["appointment_id"])
    stmt = select(models.Appointment).where(models.Appointment.id == appt_id)
    appt = session.exec(stmt).first()
    if not appt:
        return {"error": "not_found"}
    cliniko = get_cliniko_client()
    if cliniko.enabled:
        if not appt.cliniko_appointment_id:
            raise ClinikoError("Local appointment is not linked to a Cliniko appointment")
        cliniko.cancel_appointment(appt.cliniko_appointment_id, payload.get("reason"))
    appt.status = "cancelled"
    session.add(appt)
    session.commit()
    return {"appointment_id": appt.id, "status": "cancelled"}
