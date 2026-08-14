from .database import init_db, engine
from .models import Branch, Practitioner, Patient, Appointment
from sqlmodel import Session
from datetime import datetime, timedelta


def seed():
    init_db()
    with Session(engine) as session:
        # branches
        b1 = Branch(name="Gurugram Branch", address="Sector 14, Gurugram", open_time="09:00", close_time="17:00", timezone="Asia/Kolkata")
        b2 = Branch(name="Noida Branch", address="Sector 18, Noida", open_time="10:00", close_time="18:00", timezone="Asia/Kolkata")
        session.add(b1)
        session.add(b2)
        session.commit()
        session.refresh(b1)
        session.refresh(b2)

        # practitioners
        p1 = Practitioner(full_name="Dr. Suresh Mehta", specialty="General Physician", branch_id=b1.id)
        p2 = Practitioner(full_name="Dr. Anjali Rao", specialty="Dermatology", branch_id=b2.id)
        session.add(p1)
        session.add(p2)
        session.commit()

        # sample patient
        pat = Patient(full_name="Ravi Kumar", phone_e164="+919876543210")
        session.add(pat)
        session.commit()

        # seed a booked appointment to create realistic conflicts
        appt = Appointment(
            practitioner_id=p1.id,
            branch_id=b1.id,
            patient_id=pat.id,
            start_time=datetime.utcnow() + timedelta(days=1, hours=9),
            end_time=datetime.utcnow() + timedelta(days=1, hours=9, minutes=30),
            status="booked",
        )
        session.add(appt)
        session.commit()

    print("Seeded DB with branches, practitioners, and a sample patient.")
