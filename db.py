"""Compatibility shim. New code should import from alertsdb."""

from alertsdb.store import (
    DASHBOARD_HORIZONS,
    HORIZON_ASSUMPTIONS,
    count_alerts,
    get_conn,
    get_db_path,
    get_pending_alerts,
    get_pending_horizon_jobs,
    has_alert,
    init_db,
    list_alert_history,
    list_alerts,
    performance_by_horizon,
    performance_summary,
    save_alert,
    save_performance,
    set_db_path,
)

__all__ = [
    "DASHBOARD_HORIZONS",
    "HORIZON_ASSUMPTIONS",
    "count_alerts",
    "get_conn",
    "get_db_path",
    "get_pending_alerts",
    "get_pending_horizon_jobs",
    "has_alert",
    "init_db",
    "list_alert_history",
    "list_alerts",
    "performance_by_horizon",
    "performance_summary",
    "save_alert",
    "save_performance",
    "set_db_path",
]
