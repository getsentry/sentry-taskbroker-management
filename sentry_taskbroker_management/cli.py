#!/usr/bin/env python3

from __future__ import annotations

import argparse

from sentry_taskbroker_management import __version__
from sentry_taskbroker_management.scripts.pools.test_activations import (
    ProducerJobFailedError,
    ProducerJobTimeoutError,
)
from sentry_taskbroker_management.scripts.pools.test_activations import (
    add_subparser as add_send_test_activations,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentry-taskbroker-management")
    parser.add_argument(
        "--version",
        action="version",
        version=f"sentry-taskbroker-management {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_send_test_activations(subparsers)
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for sentry-taskbroker-management."""
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ProducerJobFailedError, ProducerJobTimeoutError) as exc:
        # Surface expected producer-Job failures as a clean message, matching the
        # SystemExit paths, instead of dumping a traceback.
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
