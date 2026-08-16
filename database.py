import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./uploads.db")
_is_sqlite   = DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    # SQLite: no pool config (uses StaticPool or NullPool internally)
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    # PostgreSQL / MySQL: proper connection pool for production
    engine = create_engine(
        DATABASE_URL,
        pool_size     = int(os.getenv("DB_POOL_SIZE",    "10")),
        max_overflow  = int(os.getenv("DB_MAX_OVERFLOW", "20")),
        pool_pre_ping = True,   # evict stale connections automatically
        pool_recycle  = 3600,   # recycle connections every hour
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
