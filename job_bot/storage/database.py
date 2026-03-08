from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from job_bot.models.job import Base  # also picks up Application via same Base


def get_db_url(db_path: str) -> str:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def init_db(db_path: str) -> sessionmaker[Session]:
    url = get_db_url(db_path)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    # Import Application so its table is registered on Base.metadata
    from job_bot.models.application import Application  # noqa: F401
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
