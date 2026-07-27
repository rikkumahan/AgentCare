from datetime import datetime, timedelta, timezone

from app.auth import hash_password
from app.db import SessionLocal
from app.models import (
    AppointmentSlot,
    Department,
    Doctor,
    PatientProfile,
    SlotStatus,
    User,
    UserRole,
)


def seed() -> None:
    db = SessionLocal()
    try:
        if db.query(Department).first():
            print("Seed data already present, skipping.")
            return

        cardiology = Department(
            name="Cardiology",
            description="Heart and cardiovascular care",
            active=True,
            required_document_types=["ecg"],
        )
        general = Department(
            name="General Medicine",
            description="General checkups and referrals",
            active=True,
            required_document_types=[],
        )
        db.add_all([cardiology, general])
        db.flush()

        dr_rao = Doctor(department_id=cardiology.id, name="Dr. Anitha Rao", active=True)
        dr_iyer = Doctor(department_id=general.id, name="Dr. Suresh Iyer", active=True)
        db.add_all([dr_rao, dr_iyer])
        db.flush()

        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
        for i in range(5):
            slot_start = start + timedelta(days=i)
            db.add(
                AppointmentSlot(
                    doctor_id=dr_rao.id,
                    start_time=slot_start,
                    end_time=slot_start + timedelta(minutes=30),
                    status=SlotStatus.open,
                )
            )
            db.add(
                AppointmentSlot(
                    doctor_id=dr_iyer.id,
                    start_time=slot_start,
                    end_time=slot_start + timedelta(minutes=30),
                    status=SlotStatus.open,
                )
            )

        staff_user = User(
            name="Priya Staff",
            email="staff@agentcare.test",
            password_hash=hash_password("StaffPass123!"),
            role=UserRole.staff,
        )
        patient_user = User(
            name="Test Patient",
            email="patient@agentcare.test",
            password_hash=hash_password("PatientPass123!"),
            role=UserRole.patient,
        )
        db.add_all([staff_user, patient_user])
        db.flush()
        db.add(PatientProfile(user_id=patient_user.id, phone="+91-9999999999"))

        db.commit()
        print("Seed data created: 2 departments, 2 doctors, 10 slots, 1 staff user, 1 patient user.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
