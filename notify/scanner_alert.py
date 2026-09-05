"""Daily / post-close scanner alert job (#86).

Loads one saved profile (tickers + field/min/max + window), runs the existing
chip_zscore screen, writes dashboard ``alerts`` rows, and posts Slack via the
screener helper. Failures go to logs + ``scanner_alert_runs``.

Usage:
    python -m notify.scanner_alert
    python -m notify.scanner_alert --dry-run
    python -m notify.scanner_alert --profile path.json
    python -m notify.scanner_alert save --tickers 2330,2454 --min 1.5
"""
from __future__ import annotations

import argparse
import os
import traceback
from datetime import datetime

from alertsdb import get_conn, has_alert, init_db, save_alert, set_db_path
from data import market_db
from notify import notify_job
from notify import scanner_profile as profile_mod
from web.chip_zscore import query_chip_zscore
from web.tw_calendar import taiwan_today

HIT_MARKER = "掃描每日告警"


def _slack_client(env: dict[str, str], client=None):
    if client is not None:
        return client
    from slack_sdk import WebClient

    return WebClient(token=(env.get("SLACK_BOT_TOKEN") or "").strip())


def format_empty_message(asof: str | None, skipped_duplicates: int = 0) -> str:
    when = f"（{asof}）" if asof else ""
    if skipped_duplicates:
        return f"{HIT_MARKER}{when}沒有新的符合標的（先前已通知）。"
    return f"{HIT_MARKER}{when}沒有符合的標的。"


def format_hit_message(profile: dict, hits: list[dict], asof: str | None) -> str:
    when = f"（{asof}）" if asof else ""
    cond = profile_mod.describe_condition(profile)
    lines = [f"*{HIT_MARKER}{when}* `{cond}` · {len(hits)} 檔"]
    for row in hits:
        name = row.get("stock_name") or ""
        ticker = row.get("stock_id") or ""
        z = row.get(profile["field"])
        close = row.get("close")
        z_txt = f"{z:.3f}" if isinstance(z, (int, float)) else "–"
        px = f" 收盤 {close}" if close is not None else ""
        label = f"{ticker} {name}".strip()
        lines.append(f"• `{label}`  {profile['field']}={z_txt}{px}")
    return "\n".join(lines)


def load_profile(
    *,
    path: str | None = None,
    alerts_conn=None,
    market_conn=None,
) -> tuple[dict, str]:
    """Return (profile, source). source is path|db|file|empty."""
    if path:
        loaded = profile_mod.profile_from_file(path)
        if loaded is None:
            raise FileNotFoundError(path)
        return loaded, str(path)

    for conn, label in ((alerts_conn, "db"), (market_conn, "db")):
        if conn is None:
            continue
        loaded = profile_mod.profile_from_conn(conn)
        if loaded and loaded["tickers"]:
            return loaded, label

    loaded = profile_mod.profile_from_file()
    if loaded is not None:
        return loaded, "file"
    return profile_mod.default_profile(), "empty"


def _open_alerts_writable():
    init_db()
    conn = get_conn()
    profile_mod.ensure_schema(conn)
    return conn


def _price(row: dict) -> float:
    close = row.get("close")
    try:
        return float(close) if close is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def persist_hits(
    hits: list[dict],
    profile: dict,
    asof: str,
    *,
    alerts_conn,
) -> tuple[list[dict], int]:
    """Insert new alert rows. Returns (new_hits, skipped_duplicates)."""
    pat = profile_mod.pattern_type(profile["field"])
    new_hits: list[dict] = []
    skipped = 0
    for row in hits:
        ticker = str(row.get("stock_id") or "")
        if not ticker:
            continue
        if has_alert(ticker, pat, asof):
            skipped += 1
            continue
        if save_alert(ticker, pat, asof, _price(row)) is None:
            skipped += 1
            continue
        new_hits.append(row)
    return new_hits, skipped


def post_results(
    profile: dict,
    new_hits: list[dict],
    asof: str | None,
    skipped_duplicates: int,
    *,
    env: dict[str, str] | None = None,
    client=None,
) -> str:
    env = env if env is not None else os.environ
    if not notify_job.configured(env):
        print("slack: skipped (missing secrets)")
        return "skipped"
    channel = (env.get("SLACK_CHANNEL") or "").strip()
    slack = _slack_client(env, client)
    if new_hits:
        text = format_hit_message(profile, new_hits, asof)
    else:
        text = format_empty_message(asof, skipped_duplicates)
    from notify.screener import send_to_slack

    send_to_slack(slack, channel, text)
    print("slack: sent")
    return "sent"


def run(
    *,
    profile_path: str | None = None,
    dry_run: bool = False,
    asof: str | None = None,
    env: dict[str, str] | None = None,
    client=None,
    market_conn=None,
    alerts_conn=None,
    now: datetime | None = None,
) -> dict:
    """Run the saved screen. Returns a result dict (status, hits, …)."""
    env = env if env is not None else os.environ
    owns_market = False
    owns_alerts = False
    run_date = taiwan_today(now).isoformat()

    if alerts_conn is None:
        alerts_conn = _open_alerts_writable()
        owns_alerts = True
    else:
        profile_mod.ensure_schema(alerts_conn)

    if market_conn is None:
        if not market_db.available(env):
            err = "market data unavailable (no twse_data.db and Turso not configured)"
            print(f"error: {err}")
            profile_mod.record_run(
                alerts_conn, run_date=run_date, status="error", error=err,
            )
            return {"status": "error", "error": err, "hits": [], "source": None}
        market_conn = market_db.connect(env)
        owns_market = True

    try:
        profile, source = load_profile(
            path=profile_path,
            alerts_conn=alerts_conn,
            market_conn=market_conn if market_conn is not alerts_conn else None,
        )
        print(
            f"profile source={source} tickers={len(profile['tickers'])} "
            f"field={profile['field']} window={profile['window']}"
        )
        if not profile.get("enabled", True):
            msg = "profile disabled"
            print(msg)
            profile_mod.record_run(
                alerts_conn, run_date=run_date, status="skipped", detail=msg,
            )
            return {"status": "skipped", "error": None, "hits": [], "source": source,
                    "detail": msg, "profile": profile}
        if not profile["tickers"]:
            msg = "no tickers in saved profile"
            print(f"error: {msg}")
            profile_mod.record_run(
                alerts_conn, run_date=run_date, status="error", error=msg,
            )
            return {"status": "error", "error": msg, "hits": [], "source": source,
                    "profile": profile}

        screen_asof = asof or profile.get("asof")
        payload = query_chip_zscore(
            market_conn,
            profile["tickers"],
            window=profile["window"],
            min_periods=profile["min_periods"],
            asof=screen_asof,
        )
        if payload.get("error") == "missing_tickers":
            msg = "missing_tickers"
            print(f"error: {msg}")
            profile_mod.record_run(
                alerts_conn, run_date=run_date, status="error", error=msg,
            )
            return {"status": "error", "error": msg, "hits": [], "source": source,
                    "profile": profile}

        resolved = payload.get("asof")
        hits = [row for row in payload.get("data") or [] if profile_mod.row_hits(row, profile)]
        print(
            f"screen asof={resolved} rows={len(payload.get('data') or [])} "
            f"hits={len(hits)} condition={profile_mod.describe_condition(profile)}"
        )

        if dry_run:
            for row in hits:
                print(
                    f"  dry-run hit {row.get('stock_id')} "
                    f"{profile['field']}={row.get(profile['field'])}"
                )
            if not hits:
                print(format_empty_message(resolved))
            else:
                print(format_hit_message(profile, hits, resolved))
            profile_mod.record_run(
                alerts_conn,
                run_date=run_date,
                asof=resolved,
                status="ok" if hits else "empty",
                hit_count=len(hits),
                detail="dry-run",
            )
            return {
                "status": "ok" if hits else "empty",
                "hits": hits,
                "asof": resolved,
                "source": source,
                "dry_run": True,
                "profile": profile,
                "skipped_duplicates": 0,
            }

        new_hits, skipped = persist_hits(
            hits, profile, resolved or run_date, alerts_conn=alerts_conn,
        )
        already = profile_mod.has_run_for(alerts_conn, resolved or "")
        if not new_hits and already:
            print(f"already recorded a run for {resolved}; skipping Slack")
            slack = "skipped"
        else:
            slack = post_results(
                profile, new_hits, resolved, skipped, env=env, client=client,
            )

        status = "ok" if new_hits else "empty"
        profile_mod.record_run(
            alerts_conn,
            run_date=run_date,
            asof=resolved,
            status=status,
            hit_count=len(new_hits),
            skipped_duplicates=skipped,
            detail=f"slack={slack}; source={source}",
        )
        print(
            f"done status={status} new={len(new_hits)} "
            f"skipped_dup={skipped} slack={slack}"
        )
        return {
            "status": status,
            "hits": new_hits,
            "asof": resolved,
            "source": source,
            "skipped_duplicates": skipped,
            "slack": slack,
            "profile": profile,
        }
    except Exception as exc:
        print(f"error: scanner alert failed: {exc}")
        traceback.print_exc()
        try:
            profile_mod.record_run(
                alerts_conn,
                run_date=run_date,
                status="error",
                error=str(exc)[:500],
            )
        except Exception:
            pass
        raise
    finally:
        if owns_market and market_conn is not None:
            try:
                market_conn.close()
            except Exception:
                pass
        if owns_alerts and alerts_conn is not None:
            try:
                alerts_conn.close()
            except Exception:
                pass


def _save_cli(args: argparse.Namespace) -> int:
    profile = profile_mod.parse_profile({
        "tickers": args.tickers,
        "window": args.window,
        "min_periods": args.min_periods,
        "field": args.field,
        "min": args.min,
        "max": args.max,
        "x": args.x,
        "y": args.y,
        "enabled": not args.disabled,
    })
    dest = args.file or profile_mod.DEFAULT_PROFILE_PATH
    profile_mod.write_profile_file(profile, dest)
    init_db()
    with get_conn() as conn:
        profile_mod.save_profile_conn(conn, profile)
    print(f"saved {len(profile['tickers'])} tickers → {dest} and screener.db")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="掃描條件 → 每日告警")
    sub = parser.add_subparsers(dest="cmd")
    save = sub.add_parser("save", help="write one profile (JSON + screener.db)")
    save.add_argument("--tickers", required=True, help="comma-separated stock ids")
    save.add_argument("--window", type=int, default=20)
    save.add_argument("--min-periods", type=int, default=None)
    save.add_argument("--field", default=profile_mod.DEFAULT_FIELD)
    save.add_argument("--min", type=float, default=None)
    save.add_argument("--max", type=float, default=None)
    save.add_argument("--x", default=None)
    save.add_argument("--y", default=None)
    save.add_argument("--file", default=None)
    save.add_argument("--disabled", action="store_true")
    parser.add_argument("--profile", default=os.environ.get("SCANNER_ALERT_PROFILE") or None)
    parser.add_argument("--asof", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None, help="override SCREENER_DB")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.db:
        set_db_path(args.db)
    if args.cmd == "save":
        return _save_cli(args)
    result = run(
        profile_path=args.profile,
        dry_run=args.dry_run,
        asof=args.asof,
    )
    if result.get("status") == "error":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
