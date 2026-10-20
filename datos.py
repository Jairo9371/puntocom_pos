"""Acceso SQL compartido; la transacción la decide quien llama."""

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text


db = SQLAlchemy()


def rows(sql, *, session=None, **params):
    session = db.session if session is None else session
    return [dict(row) for row in session.execute(text(sql), params).mappings().all()]


def one(sql, *, session=None, **params):
    session = db.session if session is None else session
    row = session.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def value(sql, *, session=None, **params):
    session = db.session if session is None else session
    return session.execute(text(sql), params).scalar()
