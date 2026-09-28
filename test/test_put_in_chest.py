"""put_in_chest — the deposit half of the container primitive.

take_from_chest can empty a chest; nothing could fill one. The click is the
same server-notifying `handleInventoryMouseClick`, just fired at the PLAYER's
half of the slot list (menu 27..62), where vanilla's QUICK_MOVE routes the
stack into the container.

What this pins down, because each way is silently wrong:

  * it must click player slots only — a container slot (0..26) is a *pull*,
    so depositing while the chest already holds the item would take more out;
  * a whole stack moves per click, so ask-16 with 64+16 held must move the
    16, not the 64 (same "fit the remainder" rule take uses);
  * the GUI must be verified open first, and closed on every exit path;
  * it must fail honestly when the player holds none / the click dies,
    never report a deposit that didn't move anything.

Run: python3 test/test_put_in_chest.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "test"))

import actions as actions_mod  # noqa: E402

actions_mod.time.sleep = lambda *_: None

from test_take_from_chest import AT, CHEST_SIZE, make  # noqa: E402


def clicked_slots(bridge):
    return [kw["slot"] for op, kw in bridge.calls if op == "container_click"]


def test_put_moves_the_players_stack_into_the_container():
    act, bridge = make(chest={}, player={54: ("materials_and_you", 1),
                                         55: ("sand", 32)})
    ok, note = act.put_in_chest({"item": "sand", "count": 32, "at": AT})
    assert ok is True, f"expected a deposit, got {ok!r} {note!r}"
    assert bridge._own().get("sand", 0) == 0, f"player must be out of sand: {bridge._own()}"
    assert ("sand", 32) in bridge.chest.values(), f"chest must hold it: {bridge.chest}"
    assert bridge.screen is None, "container GUI must be closed when the action returns"


def test_put_clicks_only_player_slots():
    """A container slot is a pull — clicking one here takes items back out."""
    act, bridge = make(chest={0: ("cobblestone", 16)},
                       player={55: ("sand", 32)})
    act.put_in_chest({"item": "sand", "count": 32, "at": AT})
    slots = clicked_slots(bridge)
    assert slots, bridge.calls
    assert all(s >= CHEST_SIZE for s in slots), (
        f"deposit must click the player's half (slot >= {CHEST_SIZE}), got {slots}")


def test_put_takes_the_smaller_stack_when_16_was_asked():
    act, bridge = make(chest={}, player={54: ("sand", 64), 55: ("sand", 16)})
    ok, note = act.put_in_chest({"item": "sand", "count": 16, "at": AT})
    assert ok is True, f"expected a deposit, got {ok!r} {note!r}"
    assert bridge._own()["sand"] == 64, f"exactly 16 must leave: {bridge._own()}"
    assert bridge.chest.get(0) == ("sand", 16), f"got {bridge.chest}"


def test_put_fails_when_the_player_holds_none():
    act, bridge = make(chest={}, player={54: ("materials_and_you", 1)})
    ok, note = act.put_in_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is False, f"nothing to deposit must not report success, got {ok!r}"
    assert "sand" in note, note
    assert not clicked_slots(bridge), f"must not click: {bridge.calls}"
    assert bridge.screen is None, "must not leave the GUI open on failure"


def test_put_fails_honestly_when_the_click_does_not_land():
    act, bridge = make(player={55: ("sand", 32)}, click_ok=False)
    ok, note = act.put_in_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is False, f"a dead click must not report success, got {ok!r}"
    assert "container_click" in note, note
    assert bridge.screen is None, "must close the GUI after a failed click"


def test_put_fails_when_the_block_is_not_a_container():
    act, bridge = make(open_ok=False, player={55: ("sand", 32)})
    ok, note = act.put_in_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is False, f"unopenable block must not report success, got {ok!r}"
    assert not clicked_slots(bridge), (
        "must never click with no verified GUI open")
    assert bridge.screen is None, "must not leave the GUI open on failure"


def test_put_requires_coordinates():
    act, bridge = make(player={55: ("sand", 32)})
    ok, note = act.put_in_chest({"item": "sand", "count": 8})
    assert ok is False, ok
    assert "coordinate" in note.lower() or "at" in note.lower(), note


if __name__ == "__main__":
    for fn in (test_put_moves_the_players_stack_into_the_container,
               test_put_clicks_only_player_slots,
               test_put_takes_the_smaller_stack_when_16_was_asked,
               test_put_fails_when_the_player_holds_none,
               test_put_fails_honestly_when_the_click_does_not_land,
               test_put_fails_when_the_block_is_not_a_container,
               test_put_requires_coordinates):
        fn()
        print(f"PASS {fn.__name__}")
    print("PASS")
