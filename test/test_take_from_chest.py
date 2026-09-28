"""take_from_chest must open the container and click the items out itself.

Before this, the only path was `#goto at` then "open this chest and take X,
then type .done" — so `--body player` never moved a single item on its own.
Now that op_container_click is live (see test_container_click.py), the human
hand-off is unnecessary.

Two things this pins down, both of which would silently corrupt inventory:

1. A container GUI's slot list is the container's slots FIRST and the
   player's 36 inventory slots on top (measured live: single chest = 0..26,
   player main 9..35 -> menu 27..53, hotbar 0..8 -> menu 54..62). Taking the
   first slot that holds the requested item therefore picks the player's own
   stack whenever they already hold some, and QUICK_MOVE would push it INTO
   the chest while reporting a successful pull. The listing must be split
   before clicking.

2. The GUI must be verified open before any click: a bare containerMenu is
   never null, so clicking blindly rearranges the player's own inventory.

`at` comes from the survey, which now records the block the player was
looking at rather than their feet — the actuator has to aim at a block.

Run: python3 test/test_take_from_chest.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import actions as actions_mod  # noqa: E402

actions_mod.time.sleep = lambda *_: None  # the op timings are for the game, not the test

CHEST_SIZE = 27  # slots 0..26 are the chest; 27..62 are the player


class FakeBridge:
    """Enough of the bridge to drive take_from_chest end to end."""

    def __init__(self, chest=None, player=None, open_ok=True, click_ok=True,
                 position=None, blocks=None):
        self.chest = dict(chest or {})      # menu slot -> (item, count)
        self.player = dict(player or {})    # menu slot -> (item, count)
        self.open_ok = open_ok
        self.click_ok = click_ok
        self.position = dict(position or {"x": 9.5, "y": 66.0, "z": 6.5})
        self.blocks = dict(blocks or {})    # (x, y, z) -> block id
        self.screen = None
        self.calls = []
        self.chat = []
        self.seq = 0

    # -- helpers ----------------------------------------------------------
    def _own(self):
        inv = {}
        for item, count in self.player.values():
            inv[item] = inv.get(item, 0) + count
        return inv

    def _free_player_slot(self):
        for slot in range(CHEST_SIZE, CHEST_SIZE + 36):
            if slot not in self.player:
                return slot
        return None

    def _listing(self):
        out = [{"slot": s, "item": i, "count": c} for s, (i, c) in self.chest.items()]
        out += [{"slot": s, "item": i, "count": c} for s, (i, c) in self.player.items()]
        return sorted(out, key=lambda e: e["slot"])

    # -- the bridge API ---------------------------------------------------
    def call(self, op, **kw):
        self.calls.append((op, kw))
        if op == "chat":
            self.chat.append(kw["text"])
            words = kw["text"].split()
            if len(words) == 4 and words[0] == "#goto":
                # standing next to the target, not on it: a real Baritone walk
                # ends beside the goal for an air-neighbour target.
                self.position = {"x": int(words[1]) + 0.5, "y": float(words[2]),
                                 "z": int(words[3]) + 0.5}
            return True
        if op == "getblock":
            return self.blocks.get((kw["x"], kw["y"], kw["z"]), "minecraft:air")
        if op == "chat_log":
            since = kw.get("since", 0)
            # one fresh entry per read: _baritone_idle watches for a message
            # that arrives AFTER it took its baseline, so a static log would
            # spin until its timeout and the test would hang instead of fail.
            if self.seq <= since:
                self.seq += 1
            entries = [{"n": self.seq, "message": "[Baritone] Done"}] if self.seq > since else []
            return {"entries": entries, "latest": self.seq}
        if op == "state":
            return {"open_screen": self.screen, "inventory": self._own(),
                    "position": dict(self.position)}
        if op == "close_screen":
            self.screen = None
            return {"closed": True}
        if op == "look_at":
            return True
        if op == "press":
            if kw["control"] == "use" and kw["pressed"] and self.open_ok and self.screen is None:
                self.screen = "Chest"
            return True
        if op == "container":
            return self._listing() if self.screen == "Chest" else None
        if op == "container_click":
            if not self.click_ok:
                return {"ok": False, "reason": "clicked() threw"}
            slot = kw["slot"]
            src = self.chest if slot in self.chest else self.player
            if slot not in src:
                return {"ok": False, "reason": f"no slot {slot}"}
            item, count = src.pop(slot)
            dst = self.player if src is self.chest else self.chest
            if src is self.chest:
                target = self._free_player_slot()
                if target is None:
                    src[slot] = (item, count)
                    return {"ok": False, "reason": "player inventory full"}
                dst[target] = (item, count)
            else:
                # player -> container: merge into a stack that has room, else
                # the first free chest slot — never clobber an existing one
                for s in range(CHEST_SIZE):
                    if s not in self.chest:
                        dst[s] = (item, count)
                        break
                    have, total = self.chest[s]
                    if have == item and total + count <= 64:
                        dst[s] = (have, total + count)
                        break
                else:
                    src[slot] = (item, count)
                    return {"ok": False, "reason": "container full"}
            return {"ok": True, "slot": slot}
        raise AssertionError(f"unexpected op {op!r}")


def make(chest=None, player=None, **kw):
    bridge = FakeBridge(chest=chest, player=player, **kw)
    act = actions_mod.Actions(bridge, lambda q: set(), lambda *a, **k: None, body="player")
    return act, bridge


AT = {"x": 10, "y": 66, "z": 7}


def test_take_pulls_the_container_stack_not_the_players_own():
    """Player already holds sand; the chest's sand must be the one clicked."""
    act, bridge = make(
        chest={0: ("sand", 64)},
        player={54: ("materials_and_you", 1), 55: ("sand", 32)})
    ok, note = act.take_from_chest({"item": "sand", "count": 64, "at": AT})
    assert ok is True, f"expected a successful pull, got {ok!r} {note!r}"
    assert bridge._own()["sand"] == 96, f"player should hold 96 sand, got {bridge._own()}"
    clicked = [kw["slot"] for op, kw in bridge.calls if op == "container_click"]
    assert clicked, "must have clicked something"
    assert 55 not in clicked, (
        f"slot 55 is the player's own sand — clicking it pushes it INTO the chest: {clicked}")
    assert bridge.screen is None, "container GUI must be closed when the action returns"


def test_take_when_the_item_is_not_there():
    act, bridge = make(chest={}, player={54: ("materials_and_you", 1)})
    ok, note = act.take_from_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is False, f"missing item must not report success, got {ok!r} {note!r}"
    assert "sand" in note, note
    assert not [c for c in bridge.calls if c[0] == "container_click"], (
        "must not click anything when the container lacks the item")
    assert bridge.screen is None, "must not leave the GUI open on failure"


def test_take_when_the_block_is_not_a_container():
    act, bridge = make(open_ok=False)
    ok, note = act.take_from_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is False, f"unopenable block must not report success, got {ok!r}"
    assert "container" in note.lower() or "open" in note.lower(), note
    assert not [c for c in bridge.calls if c[0] == "container_click"], (
        "must never click with no verified GUI open")


def test_take_fails_honestly_when_the_click_does_not_land():
    act, bridge = make(chest={0: ("sand", 64)}, click_ok=False)
    ok, note = act.take_from_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is False, f"a dead click must not report success, got {ok!r}"
    assert "container_click" in note, note
    assert bridge.screen is None, "must close the GUI after a failed click"


def test_take_walks_to_an_air_neighbour_not_onto_the_container():
    """Baritone's allowBreak mines the goal block itself.

    Observed live: `#goto 10 66 7` at the chest's own block broke the chest
    and dropped it (contents lost), because standing on GoalBlock is the goal.
    So the walk must target an air block BESIDE the container.
    """
    act, bridge = make(chest={0: ("sand", 64)},
                       position={"x": 30.0, "y": 66.0, "z": 30.0},
                       blocks={(10, 66, 7): "minecraft:chest[facing=north]"})
    ok, note = act.take_from_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is True, f"expected a pull, got {ok!r} {note!r}"
    gotos = [t for t in bridge.chat if t.startswith("#goto")]
    assert gotos, bridge.chat
    assert not any(g.startswith("#goto 10 66 7") for g in gotos), (
        f"pathing onto the container breaks it: {gotos}")
    x, y, z = gotos[0].split("#goto ")[1].split()
    assert (int(x), int(y), int(z)) != (AT["x"], AT["y"], AT["z"]), gotos
    assert abs(int(x) - AT["x"]) <= 1 and abs(int(z) - AT["z"]) <= 1, (
        f"target must be adjacent to the container, got {gotos[0]}")


def test_take_does_not_over_pull():
    """Ask for 64 from a chest holding 16+64: exactly 64 must arrive.

    _split_listing returns slots descending, so a naive next() grabs the 16
    first and then still needs the 64 — both move and the player ends up with
    80. The stack choice must fit the remainder.
    """
    act, bridge = make(chest={0: ("sand", 64), 1: ("sand", 16)},
                       player={54: ("materials_and_you", 1)})
    ok, note = act.take_from_chest({"item": "sand", "count": 64, "at": AT})
    assert ok is True, f"expected a pull, got {ok!r} {note!r}"
    assert bridge._own()["sand"] == 64, f"asked 64, must have exactly 64: {bridge._own()}"
    assert bridge.chest.get(1) == ("sand", 16), f"the 16 stack must be left alone: {bridge.chest}"


def test_take_does_not_path_at_all_when_already_in_reach():
    act, bridge = make(chest={0: ("sand", 64)})   # starts at 9.5,66,6.5
    ok, note = act.take_from_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is True, f"expected a pull, got {ok!r} {note!r}"
    assert not [t for t in bridge.chat if t.startswith("#goto")], bridge.chat


def test_take_grabs_the_smallest_stack_when_none_fits_the_remainder():
    """A whole stack must move, so ask-8 can overshoot — but by the least."""
    act, bridge = make(chest={0: ("sand", 64), 1: ("sand", 32)},
                       player={54: ("materials_and_you", 1)})
    ok, note = act.take_from_chest({"item": "sand", "count": 8, "at": AT})
    assert ok is True, f"expected a pull, got {ok!r} {note!r}"
    assert bridge._own()["sand"] == 32, f"must take the 32 stack: {bridge._own()}"
    assert bridge.chest.get(0) == ("sand", 64), f"big stack untouched: {bridge.chest}"


def test_split_listing_separates_container_from_player():
    listing = [{"slot": 0, "item": "sand", "count": 64},
               {"slot": 27, "item": "bow", "count": 1},
               {"slot": 54, "item": "materials_and_you", "count": 1},
               {"slot": 55, "item": "sand", "count": 32}]
    own = {"sand": 32, "bow": 1, "materials_and_you": 1}
    here, mine = actions_mod.Actions._split_listing(listing, own)
    assert [e["slot"] for e in here] == [0], here
    assert sorted(e["slot"] for e in mine) == [27, 54, 55], mine


def test_take_requires_coordinates():
    act, bridge = make(chest={0: ("sand", 64)})
    ok, note = act.take_from_chest({"item": "sand", "count": 8})
    assert ok is False, ok
    assert "coordinate" in note.lower() or "at" in note.lower(), note


if __name__ == "__main__":
    for fn in (test_take_pulls_the_container_stack_not_the_players_own,
               test_take_when_the_item_is_not_there,
               test_take_when_the_block_is_not_a_container,
               test_take_fails_honestly_when_the_click_does_not_land,
               test_take_walks_to_an_air_neighbour_not_onto_the_container,
               test_take_does_not_path_at_all_when_already_in_reach,
               test_take_does_not_over_pull,
               test_take_grabs_the_smallest_stack_when_none_fits_the_remainder,
               test_split_listing_separates_container_from_player,
               test_take_requires_coordinates):
        fn()
        print(f"PASS {fn.__name__}")
    print("PASS")
