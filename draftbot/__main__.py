"""Entry point: ``uv run python -m draftbot``."""
from __future__ import annotations

import logging
import os
import sys

import discord

from .bot import DraftBot


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit(
            "DISCORD_TOKEN is not set.\n"
            "Create a bot at https://discord.com/developers/applications "
            "(New Application -> Bot -> Reset Token), then:\n"
            "  export DISCORD_TOKEN=your-token-here"
        )
    discord.utils.setup_logging(level=logging.INFO)
    DraftBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
