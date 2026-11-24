"""Failures whose message is written for the user."""

from __future__ import annotations


class UserError(Exception):
    """A failure whose message can be shown on screen as is.

    Every other exception may carry paths, codes or library text, so the screen
    gets a fixed sentence instead and the details go to app.log.
    """
