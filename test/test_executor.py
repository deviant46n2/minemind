"""Executor interface — the seam between deciding and doing.

The brain (daemon loop + planner) emits action steps; an executor runs them
against Minecraft and reports (ok, note). Two backends exist:

  * player body (Actions in actions.py): the bot's own client joins over LAN,
    acts through the Minescript bridge. Needs nothing on the host.
  * companion body (same Actions class, body="companion"): /agent commands
    drive a PlayerEngine AutomatoneEntity on the host. Needs agentlink +
    PlayerEngine + Player2NPC jars installed host-side (see
    bridge/PLAYERENGINE.md) — NOT deployed yet, so this backend is currently
    aspirational: do not add a second executor class until its backend can
    run (an untestable executor is fiction, not architecture).

Run: python3 test/test_executor.py
"""
import sys
from abc import ABC, abstractmethod
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import actions as actions_mod  # noqa: E402
from executor import ActionExecutor  # noqa: E402


class FakeBridge:
    def __init__(self):
        self.calls = []

    def call(self, op, **kw):
        self.calls.append((op, kw))
        return {"ok": True}


def texts(bridge):
    return [kw.get("text") for op, kw in bridge.calls if op == "chat"]


def test_interface_requires_run_and_interrupt():
    class HalfBaked(ActionExecutor):
        def run(self, step):
            return True, "ok"
    try:
        HalfBaked()
        raise AssertionError("ActionExecutor must not instantiate without interrupt()")
    except TypeError:
        pass


def test_actions_is_an_executor():
    bridge = FakeBridge()
    act = actions_mod.Actions(bridge, lambda q: set(), lambda *a, **k: None,
                              body="player")
    assert isinstance(act, ActionExecutor), (
        "the existing implementation must satisfy the interface as-is")


def test_interrupt_halts_the_player_body():
    bridge = FakeBridge()
    act = actions_mod.Actions(bridge, lambda q: set(), lambda *a, **k: None,
                              body="player")
    assert act.interrupt() is True
    assert "#stop" in texts(bridge), texts(bridge)


def test_interrupt_halts_the_companion_body():
    bridge = FakeBridge()
    act = actions_mod.Actions(bridge, lambda q: set(), lambda *a, **k: None,
                              body="companion")
    assert act.interrupt() is True
    assert "/agent stop" in texts(bridge), texts(bridge)


if __name__ == "__main__":
    test_interface_requires_run_and_interrupt()
    print("PASS test_interface_requires_run_and_interrupt")
    test_actions_is_an_executor()
    print("PASS test_actions_is_an_executor")
    test_interrupt_halts_the_player_body()
    print("PASS test_interrupt_halts_the_player_body")
    test_interrupt_halts_the_companion_body()
    print("PASS test_interrupt_halts_the_companion_body")
    print("PASS")
