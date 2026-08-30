"""screener.db access for Slack, harness, and later the website."""

from alertsdb.store import (
    count_alerts,
    get_conn,
    get_db_path,
    get_pending_alerts,
    get_pending_horizon_jobs,
    has_alert,
    init_db,
    list_alert_history,
    list_alerts,
    performance_summary,
    save_alert,
    save_performance,
    set_db_path,
)

__all__ = [
    "count_alerts",
    "get_conn",
    "get_db_path",
    "get_pending_alerts",
    "get_pending_horizon_jobs",
    "has_alert",
    "init_db",
    "list_alert_history",
    "list_alerts",
    "performance_summary",
    "save_alert",
    "save_performance",
    "set_db_path",
]
