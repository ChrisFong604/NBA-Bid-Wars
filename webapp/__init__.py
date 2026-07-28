"""Web app shell around the pure draftbot engine (see PROTOCOL.md).

Never imports ``draftbot.bot`` or ``draftbot.ui``; never reimplements a game
rule — ``draftbot.engine.apply`` decides everything.
"""
