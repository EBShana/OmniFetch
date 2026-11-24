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

    p_serve = sub.add_parser("serve", help="run the Web API and UI")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)

    p115 = sub.add_parser("115-login", help="扫码登录 115 网盘（下载器实例需 type=p115）")
    p115.add_argument("name", help="downloaders.d 中的实例名")

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

        from .app import create_app

        build_context(settings)  # ensure schema exists before serving
        uvicorn.run(create_app(settings), host=args.host, port=args.port)

    if args.command == "115-login":
        from .config import load_plugin_configs
        from .downloaders.p115 import qrcode_login

        _, downloader_cfgs = load_plugin_configs(settings.config_dir)
        cfg = downloader_cfgs.get(args.name)
        if not cfg or cfg.get("type") != "p115":
            print(f"错误：config/downloaders.d 中不存在 type=p115 的实例 '{args.name}'")
            return 1
        cookie_file = cfg.get("cookie_file") or ""
        if not cookie_file:
            print("错误：该实例未配置 cookie_file")
            return 1
        try:
            qrcode_login(cookie_file, proxy=cfg.get("proxy") or None)
        except Exception as exc:
            print(f"登录失败：{exc}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
