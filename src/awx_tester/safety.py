"""Gates that keep mutating and disruptive checks away from systems that did not opt in.

Two independent switches must agree before anything beyond read-only runs:

1. the target's ``max_level`` in the targets file (``protected: true`` pins it to readonly);
2. the ``--level`` asked for on the command line.

Disruptive runs additionally need the target name typed back, either with
``--confirm NAME`` or at the interactive prompt.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from .config import Target
from .model import Level


class SafetyError(Exception):
    pass


def authorize(
    target: Target,
    requested: Level,
    confirm: str | None = None,
    prompt: Callable[[str], str] | None = None,
) -> None:
    allowed = target.effective_max_level
    if not allowed.allows(requested):
        why = "it is marked protected" if target.protected else f"its max_level is {allowed.value}"
        raise SafetyError(
            f"refusing a {requested.value} run against {target.name!r}: {why}. "
            f"Raise max_level in the targets file if this installation may be changed."
        )
    if requested is Level.DISRUPTIVE:
        if confirm is not None:
            if confirm != target.name:
                raise SafetyError(f"--confirm {confirm!r} does not match target name {target.name!r}")
            return
        if prompt is None and not sys.stdin.isatty():
            raise SafetyError("disruptive runs need --confirm <target name> when not interactive")
        ask = prompt or input
        answer = ask(
            f"Disruptive tests will delete pods of {target.name!r} "
            f"({target.url}, namespace {target.kubernetes.namespace}). Type the target name to go on: "
        )
        if answer.strip() != target.name:
            raise SafetyError("confirmation did not match; nothing was run")
