from sqlalchemy import text
from db.session import engine


def get_conn():
    with engine.connect() as conn:
        yield conn
