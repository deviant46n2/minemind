"""say() — user-visible messages must reach the human's screen.

`echo` only displays on the bot's own client. Joined over LAN (player or
companion body), the human host never sees it — craft requests, manual
escalations and progress notes vanish silently while the daemon blocks
waiting for a `.done` the player was never asked for. say() broadcasts
through world chat instead; the chat_log parsers already skip our own
"[agent]" lines, so the bot never reacts to itself.

Run: python3 test/test_say_broadcast.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import actions as actions_mod  # noqa: E402
import daemon as daemon_mod  # noqa: E402


class FakeBridge:
    def __init__(self):
        self.calls = []

    def call(self, op, **kw):
        self.calls.append((op, kw))
        return {"ok": True}


def ops_of(bridge, op):
    return [kw for name, kw in bridge.calls if name == op]


def test_actions_say_broadcasts_to_world_chat():
    bridge = FakeBridge()
    act = actions_mod.Actions(bridge, lambda q: set(), lambda *a, **k: None,
                              body="player")
    act.say("[agent] please CRAFT 1x backpack, then type .done")
    assert ops_of(bridge, "chat"), f"say() must broadcast, got {bridge.calls}"
    assert not ops_of(bridge, "echo"), (
        f"say() must not use screen-only echo: {bridge.calls}")


def test_daemon_say_broadcasts_to_world_chat():
    bridge = FakeBridge()
    daemon_mod.say(bridge, "[agent] working on: Spout")
    assert ops_of(bridge, "chat"), f"say() must broadcast, got {bridge.calls}"
    assert not ops_of(bridge, "echo"), (
        f"say() must not use screen-only echo: {bridge.calls}")


if __name__ == "__main__":
    test_actions_say_broadcasts_to_world_chat()
    print("PASS test_actions_say_broadcasts_to_world_chat")
    test_daemon_say_broadcasts_to_world_chat()
    print("PASS test_daemon_say_broadcasts_to_world_chat")
    print("PASS")
