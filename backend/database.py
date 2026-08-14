from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text
import os

DB_FILE = os.environ.get("VOICEAI_DB", "./voiceai.db")
DATABASE_URL = f"sqlite:///{DB_FILE}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


def init_db():
    SQLModel.metadata.create_all(engine)
    # Existing local SQLite databases predate the Cliniko mapping columns.
    # `create_all` does not alter a table, so make the additive dev migration
    # explicit. Production Postgres should use a real Alembic migration.
    if DATABASE_URL.startswith("sqlite"):
        additions = {
            "branch": {"cliniko_business_id": "TEXT"},
            "practitioner": {"cliniko_practitioner_id": "TEXT"},
            "appointment": {"cliniko_appointment_id": "TEXT"},
        }
        with engine.begin() as connection:
            for table, columns in additions.items():
                existing = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
                for column, sql_type in columns.items():
                    if column not in existing:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


def get_session():
    with Session(engine) as session:
        yield session
