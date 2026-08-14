from typing import Optional
from sqlmodel import SQLModel, Field
from datetime import datetime


class Branch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    address: Optional[str] = None
    open_time: Optional[str] = None  # e.g. "09:00"
    close_time: Optional[str] = None  # e.g. "17:00"
    timezone: Optional[str] = None  # e.g. "Asia/Kolkata"
    cliniko_business_id: Optional[str] = Field(default=None, index=True)


class Practitioner(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: str
    specialty: Optional[str] = None
    branch_id: Optional[int] = None
    cliniko_practitioner_id: Optional[str] = Field(default=None, index=True)


class Patient(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: Optional[str] = None
    phone_e164: Optional[str] = None
    external_id: Optional[str] = None


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    practitioner_id: int
    branch_id: int
    patient_id: Optional[int] = None
    start_time: datetime
    end_time: datetime
    status: str = "booked"  # booked, cancelled, rescheduled
    call_session_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    cliniko_appointment_id: Optional[str] = Field(default=None, index=True)


class CallSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    call_session_id: str
    phone_e164: Optional[str] = None
    direction: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    outcome: Optional[str] = None


class IdempotencyKey(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    result_reference: Optional[str] = None


class Metric(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    tool: Optional[str] = None
    latency_s: Optional[float] = None
    status: Optional[int] = None
    payload: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
