"""Database layer — connection pool and table definitions.

Usage:
    from wekruit_matching.db import get_connection
    with get_connection() as conn:
        conn.execute("SELECT 1")
"""
from .connection import get_connection, get_pool
from .tables import feedback_table, jobs_table, metadata, user_profiles_table

__all__ = [
    "get_pool",
    "get_connection",
    "metadata",
    "jobs_table",
    "user_profiles_table",
    "feedback_table",
]
