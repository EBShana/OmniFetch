"""CLI entry point: init-db | run [--once] | serve | 115-login | hash-password."""
from __future__ import annotations

import argparse
import logging
import sys
import threading

from .config import load_settings
from .core import db
from .scheduler.runner import build_context, run_forever, run_once

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omnifetch")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create/upgrade the database schema")

    p_run = sub.add_parser("run", help="run the scheduler loop")
    p_run.add_argument("--once", action="store_true", help="run a single tick and exit")

    p_serve = sub.add_parser("serve", help="run the Web API and UI")
    # LAN by default. Startup warns (does not refuse) when neither the API
    # token nor a UI login password is configured.
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)

    p115 = sub.add_parser("115-login", help="扫码登录 115 网盘（下载器实例需 type=p115）")
    p115.add_argument("name", help="downloaders.d 中的实例名")

    sub.add_parser(
        "hash-password",
        help="交互式生成 Web UI 登录密码哈希（写入 config/omnifetch.toml）",
    )
    return parser


def is_loopback(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def warn_if_exposed(host: str, token: str, ui_password_hash: str = "") -> bool:
    """Warn when a non-loopback bind has neither API token nor UI login."""
    if token or ui_password_hash or is_loopback(host):
        return False
    log.warning(
        "serving on %s without OMNIFETCH_TOKEN and without a Web UI login "
        "password: anyone who can reach this port can edit plugin configs "
        "and control downloads", host,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
            ctx.wait_crawlers()  # --once still finishes the crawl it started
            print("one scheduler tick completed")
            return 0
        print(f"scheduler started (poll interval {settings.poll_interval}s)")
        run_forever(ctx)

    if args.command == "serve":
        import uvicorn

        from .app import create_app

        warn_if_exposed(args.host, settings.token, settings.ui_password_hash)
        ctx = build_context(settings)  # one context shared by UI and scheduler
        stop_event = threading.Event()
        threading.Thread(
            target=run_forever, args=(ctx, stop_event),
            name="omnifetch-scheduler", daemon=True,
        ).start()
        print(f"scheduler started in-process (poll interval {settings.poll_interval}s)")
        try:
            uvicorn.run(create_app(settings, ctx), host=args.host, port=args.port)
        finally:
            stop_event.set()
            ctx.stop_crawlers()

    if args.command == "hash-password":
        import getpass

        from .core.security import hash_password

        password = getpass.getpass("Web UI 登录密码: ")
        if not password:
            print("错误：密码不能为空")
            return 1
        if getpass.getpass("再输入一次确认: ") != password:
            print("错误：两次输入不一致")
            return 1
        print(hash_password(password))
        print("\n把上面这行哈希填入 config/omnifetch.toml 的 ui_password_hash，"
              "\n或设为环境变量 OMNIFETCH_UI_PASSWORD_HASH（仅存哈希，不存明文）。")
        return 0

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
            qrcode_login(cookie_file)
        except Exception as exc:
            print(f"登录失败：{exc}")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
