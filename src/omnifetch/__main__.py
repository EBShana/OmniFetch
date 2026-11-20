"""CLI entry point: init-db | run [--once] | serve."""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import load_settings
from .core import db
from .scheduler.runner import build_context, run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omnifetch")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create/upgrade the database schema")

    p_run = sub.add_parser("run", help="run the scheduler loop")
    p_run.add_argument("--once", action="store_true", help="run a single tick and exit")

    p_serve = sub.add_parser("serve", help="run the Web API")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()

    if args.command == "init-db":
        ctx = build_context(settings)
        print(f"database ready at {settings.db_path}")
        return 0

    if args.command == "run":
        ctx = build_context(settings)
        if args.once:
            run_once(ctx)
            print("one scheduler tick completed")
            return 0
        print(f"scheduler started (poll interval {settings.poll_interval}s)")
        while True:
            try:
                run_once(ctx)
            except Exception:
                logging.getLogger(__name__).exception("scheduler tick failed")
            time.sleep(settings.poll_interval)

    if args.command == "serve":
        import uvicorn

        from .webapi.app import create_app

        build_context(settings)  # ensure schema exists before serving
        uvicorn.run(create_app(settings), host=args.host, port=args.port)

    return 0


if __name__ == "__main__":
    sys.exit(main())
