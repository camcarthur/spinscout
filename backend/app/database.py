from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def migrate_large_strava_ids() -> None:
    if engine.dialect.name != "postgresql":
        return

    targets = (
        ("users", "strava_athlete_id"),
        ("activities", "strava_activity_id"),
    )

    with engine.begin() as conn:
        for table_name, column_name in targets:
            data_type = conn.execute(
                text(
                    """
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                      AND column_name = :column_name
                    """
                ),
                {"table_name": table_name, "column_name": column_name},
            ).scalar_one_or_none()

            if data_type == "integer":
                conn.execute(
                    text(f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" TYPE BIGINT')
                )


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
