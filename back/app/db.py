from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    future=True,
)


def test_connection() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))