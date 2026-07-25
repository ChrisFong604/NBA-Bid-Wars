"""Guard against shadowing discord.py internals on the Client subclass."""
import discord

from draftbot.bot import DraftBot

ALLOWED_OVERRIDES = {"setup_hook", "on_ready"}


def test_no_accidental_client_shadowing():
    # The gateway calls Client.dispatch('connect') positionally; overriding it
    # (or any other Client method) with a different signature crashes login.
    ours = {n for n in DraftBot.__dict__ if not n.startswith("__")}
    shadowed = ours & set(dir(discord.Client)) - ALLOWED_OVERRIDES
    assert not shadowed, f"DraftBot shadows discord.Client attrs: {shadowed}"
