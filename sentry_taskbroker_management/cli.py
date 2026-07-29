#!/usr/bin/env python3

from __future__ import annotations

import click

from sentry_taskbroker_management import __version__
from sentry_taskbroker_management.scripts.pools.healthcheck import pool_healthcheck

COMMANDS: list[click.Command] = [
    pool_healthcheck,
]


@click.group()
@click.version_option(version=__version__, prog_name="sentry-taskbroker-management")
def main() -> None:
    """
    CLI entrypoint for sentry-taskbroker-management.
    """
    pass


for command in COMMANDS:
    main.add_command(command)

if __name__ == "__main__":
    main()
