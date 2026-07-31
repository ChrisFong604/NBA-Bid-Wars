"""Guard against shadowing discord.py internals on the Client subclass."""
import discord

from draftbot.bot import DraftBot
from draftbot.models import Config

ALLOWED_OVERRIDES = {"setup_hook", "on_ready"}


def test_default_pool_and_era():
    # /draft create's options and the web create form both default to the
    # legends pool from the 2000s onward — Config() must agree.
    assert Config().pool_depth == "legends"
    assert (Config().era_start, Config().era_end) == (2000, 2020)


def test_no_accidental_client_shadowing():
    # The gateway calls Client.dispatch('connect') positionally; overriding it
    # (or any other Client method) with a different signature crashes login.
    ours = {n for n in DraftBot.__dict__ if not n.startswith("__")}
    shadowed = ours & set(dir(discord.Client)) - ALLOWED_OVERRIDES
    assert not shadowed, f"DraftBot shadows discord.Client attrs: {shadowed}"
