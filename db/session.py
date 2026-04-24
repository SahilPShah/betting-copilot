import os
from contextlib import contextmanager
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()
engine = create_engine(os.getenv("DATABASE_URL"))


@contextmanager
def get_conn():
    with engine.connect() as conn:
        yield conn
