"""Local SQLite pragmas. Do not apply these to Turso / libSQL HTTP connections."""


def configure_local(conn, *, wal: bool = True) -> None:
    """Short writer waits, and WAL so a dashboard reader does not block catch-up jobs."""
    conn.execute("PRAGMA busy_timeout = 5000")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
