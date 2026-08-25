#!/usr/bin/env python3

from __future__ import annotations

import argparse

from sentry_taskbroker_management import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentry-taskbroker-management")
    parser.add_argument(
        "--version",
        action="version",
        version=f"sentry-taskbroker-management {__version__}",
    )
    parser.add_subparsers(dest="command", required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for sentry-taskbroker-management."""
    build_parser().parse_args(argv)


if __name__ == "__main__":
    main()
