from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def build_engine(db_url: str):
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, future=True, connect_args=connect_args)


def build_session_factory(db_url: str) -> sessionmaker[Session]:
    engine = build_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False, future=True)
