"""Command line interface: ``garmin-data <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from . import annotations as ann
from . import build
from .auth import AuthRequired, get_client
from .config import Settings, load_settings
from .export import export_tables
from .gaps import find_gaps
from .store import Store
from .sync import incremental_window, sync

log = logging.getLogger("garmin_data")

# Exit codes (stable; safe to use from schedulers / scripts)
EXIT_OK = 0  # everything requested was fetched and normalized
EXIT_ABORTED = 1  # fatal: auth failure, rate limit, failed rebuild, ...
EXIT_USAGE = 2  # invalid arguments
EXIT_PARTIAL = 3  # finished, but some requests or normalizations failed


def _setup_logging(settings: Settings, verbose: bool) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logfile = logging.FileHandler(settings.data_dir / "garmin-data.log", encoding="utf-8")
    logfile.setLevel(logging.INFO)
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    root.addHandler(logfile)
    # The Garmin client is chatty at DEBUG and may log URLs; keep it at WARNING.
    for noisy in ("garminconnect", "urllib3", "curl_cffi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from e


def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    client = get_client(settings, interactive=True)
    name = client.get_full_name() or "unknown"
    print(f"Logged in as {name}; session cached in {settings.tokenstore}")
    return 0


def cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    with Store(settings.db_path) as store:
        if args.date_from:
            start, end = args.date_from, args.date_to or date.today()
        elif args.date_to:
            print("--to requires --from", file=sys.stderr)
            return EXIT_USAGE
        else:
            start, end = incremental_window(store, settings)
        # Without an explicit range, also retry data missing from earlier syncs.
        fill_gaps = args.date_from is None or args.fill_gaps
        g = get_client(settings)
        result = sync(g, store, settings, start, end, refresh=args.refresh, fill_gaps=fill_gaps)
    s = result.stats
    print(
        f"Synced {result.start} .. {result.end}: "
        f"{result.activities} activities, {result.days} days | "
        f"raw new={s.new} unchanged={s.unchanged} empty={s.empty} errors={s.errors}"
    )
    if result.gaps_retried or result.gaps_given_up:
        print(
            f"Earlier gaps: {result.gaps_retried} retried, "
            f"{result.gaps_given_up} given up after {settings.max_fetch_attempts} attempts"
        )
    if s.failed:
        print("Failed requests (see `garmin-data status` / log):", file=sys.stderr)
        for item in s.failed[:20]:
            print(f"  {item}", file=sys.stderr)
    for err in result.normalize_errors[:20]:
        print(f"Normalization failed: {err}", file=sys.stderr)
    if result.aborted:
        print(f"Aborted: {result.aborted}", file=sys.stderr)
        return EXIT_ABORTED
    if result.status == "partial":
        print("Sync incomplete (exit code 3); run `garmin-data sync` again later.", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


def cmd_rebuild(settings: Settings, args: argparse.Namespace) -> int:
    with Store(settings.db_path) as store:
        try:
            report = build.rebuild(store)
        except build.RebuildError as e:
            print(f"Rebuild failed and was rolled back (data unchanged): {e}", file=sys.stderr)
            return EXIT_ABORTED
    print(f"Rebuilt from raw: {report.activities} activities, {report.days} days")
    return EXIT_OK


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    if not settings.db_path.exists():
        print(f"No database yet at {settings.db_path}. Run `garmin-data sync`.")
        return 0
    with Store(settings.db_path) as store:
        q = store.con.execute
        print(f"Database: {settings.db_path}")
        for table, date_col in (
            ("activity", "date"),
            ("activity_lap", None),
            ("activity_split", None),
            ("daily_health", "date"),
            ("annotation", "date"),
            ("raw_payload", None),
        ):
            if date_col:
                n, lo, hi = q(
                    f"SELECT count(*), min({date_col}), max({date_col}) FROM {table}"
                ).fetchone()
                print(f"  {table:16} {n:7}  {lo or '-'} .. {hi or '-'}")
            else:
                (n,) = q(f"SELECT count(*) FROM {table}").fetchone()
                print(f"  {table:16} {n:7}")
        runs = q(
            "SELECT started_at, date_from, date_to, status FROM sync_run "
            "ORDER BY started_at DESC LIMIT 5"
        ).fetchall()
        if runs:
            print("Recent sync runs (UTC):")
            for started, lo, hi, status in runs:
                print(f"  {started:%Y-%m-%d %H:%M}  {lo} .. {hi}  {status}")
        gaps = find_gaps(store, settings.max_fetch_attempts, date.today())
        print(
            f"Missing data in synced ranges: {gaps.pending} pending retry, {gaps.given_up} given up"
        )
        errors = q(
            "SELECT endpoint, source_key, error FROM sync_log WHERE status='error' "
            "ORDER BY logged_at DESC LIMIT 10"
        ).fetchall()
        if errors:
            print("Recent errors:")
            for endpoint, key, err in errors:
                print(f"  {endpoint} {key}: {(err or '')[:120]}")
    return 0


def cmd_export(settings: Settings, args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser() if args.out else settings.exports_dir
    with Store(settings.db_path) as store:
        paths = export_tables(store, out, args.format)
    for p in paths:
        print(p)
    return 0


def cmd_web(settings: Settings, args: argparse.Namespace) -> int:
    from .web import serve

    if not settings.db_path.exists():
        print(f"No database yet at {settings.db_path}. Run `garmin-data sync`.", file=sys.stderr)
        return EXIT_USAGE
    try:
        serve(settings.db_path, args.host, args.port)
    except OSError as e:
        print(f"Cannot start web server on {args.host}:{args.port}: {e}", file=sys.stderr)
        return EXIT_ABORTED
    return EXIT_OK


def cmd_note(settings: Settings, args: argparse.Namespace) -> int:
    fields = {k: getattr(args, k, None) for k in ann.FIELDS}
    with Store(settings.db_path) as store:
        if args.note_cmd == "add":
            if args.activity is None and args.date is None:
                print("Give --activity and/or --date", file=sys.stderr)
                return 2
            nid = ann.add_annotation(store, activity_id=args.activity, day=args.date, **fields)
            print(f"Added note {nid}")
        elif args.note_cmd == "edit":
            ann.update_annotation(store, args.id, **fields)
            print(f"Updated note {args.id}")
        elif args.note_cmd == "delete":
            ann.delete_annotation(store, args.id)
            print(f"Deleted note {args.id}")
        else:
            for n in ann.list_annotations(store, args.date_from, args.date_to):
                parts = [f"{k}={n[k]}" for k in ann.FIELDS if n[k] is not None]
                print(
                    f"#{n['id']} {n['date'] or '-'} activity={n['activity_id'] or '-'} "
                    + " ".join(parts)
                )
    return 0


def _note_fields(p: argparse.ArgumentParser) -> None:
    p.add_argument("--rpe", type=int, help="1-10")
    p.add_argument("--fatigue", type=int)
    p.add_argument("--legs")
    p.add_argument("--motivation", type=int)
    p.add_argument("--soreness", dest="soreness_or_pain")
    p.add_argument("--notes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garmin-data", description=__doc__)
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="log in and cache the Garmin session")

    p = sub.add_parser("sync", help="sync a date range, or incrementally without arguments")
    p.add_argument("--from", dest="date_from", type=_date)
    p.add_argument("--to", dest="date_to", type=_date)
    p.add_argument("--refresh", action="store_true", help="re-fetch data already stored")
    p.add_argument(
        "--fill-gaps",
        action="store_true",
        help="with --from: also retry data missing from earlier syncs (default without --from)",
    )

    sub.add_parser("rebuild", help="rebuild normalized tables from raw payloads (offline)")
    sub.add_parser("status", help="show what is stored")

    p = sub.add_parser("export", help="export tables to parquet/csv")
    p.add_argument("--format", choices=("parquet", "csv"), default="parquet")
    p.add_argument("--out", help="output directory (default: <data_dir>/exports)")

    p = sub.add_parser("web", help="local read-only web view of activities")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("note", help="manual training annotations")
    note = p.add_subparsers(dest="note_cmd", required=True)
    add = note.add_parser("add")
    add.add_argument("--activity", type=int)
    add.add_argument("--date", type=_date)
    _note_fields(add)
    edit = note.add_parser("edit")
    edit.add_argument("id", type=int)
    _note_fields(edit)
    delete = note.add_parser("delete")
    delete.add_argument("id", type=int)
    lst = note.add_parser("list")
    lst.add_argument("--from", dest="date_from", type=_date)
    lst.add_argument("--to", dest="date_to", type=_date)
    return parser


COMMANDS = {
    "login": cmd_login,
    "sync": cmd_sync,
    "rebuild": cmd_rebuild,
    "status": cmd_status,
    "export": cmd_export,
    "note": cmd_note,
    "web": cmd_web,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    _setup_logging(settings, args.verbose)
    try:
        return COMMANDS[args.command](settings, args)
    except AuthRequired as e:
        print(str(e), file=sys.stderr)
        return EXIT_ABORTED
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
