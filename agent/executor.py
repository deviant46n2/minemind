"""Executor interface — the seam between deciding and doing.

The brain (daemon loop + planner in daemon.py, prompts in prompts.py, LLM in
llm.py) emits action steps as plain dicts ({"action": name, ...params}).
An executor runs one step against Minecraft and reports (ok: bool, note).

Backends:
  * player body — Actions in actions.py with body="player": the bot's own
    client joins over LAN and acts through the Minescript bridge. Needs
    nothing installed on the host. This is the live path.
  * companion body — the same Actions class with body="companion": /agent
    chat commands drive a PlayerEngine AutomatoneEntity living on the host
    (see bridge/PLAYERENGINE.md for the full integration analysis). Needs
    agentlink + PlayerEngine + Player2NPC jars host-side; until those are
    deployed and smoke-tested, do NOT add a second executor class here.

Rule: PlayerEngine must never know CoopBot exists. The brain knows actions;
the executor knows backends. The LLM never touches movement — it emits
action dicts, the executor translates them (validated dispatch in
Actions.run, /agent command lines for the companion).
"""
from abc import ABC, abstractmethod


class ActionExecutor(ABC):
    """One step in, (ok, note) out. Interrupt halts whatever is running."""

    @abstractmethod
    def run(self, step):
        """Execute an action step dict. Returns (ok: bool, note: str)."""

    @abstractmethod
    def interrupt(self):
        """Halt the body now (Baritone #stop / /agent stop). Returns True
        if the halt command was issued."""
