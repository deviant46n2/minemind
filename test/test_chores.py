"""Chore loop — the bot works from conversation, not from quests.

The human does quests manually; the bot's job is whatever the player says
in chat. That needs four pieces, each pinned here:

  * extract_player_lines: only lines FROM the player's username count as
    instructions — the bot's own <CoopBot> lines, [agent] broadcasts and
    server noise must never steer it;
  * ask: the "I don't know, so I'll ask" step — broadcast the question,
    wait for the player's reply, hand the answer back. Silence asks once
    more, then gives up (never blocks the loop forever);
  * chore prompt: instruction + recent conversation + live state, with NO
    quest frontier (quests must not leak back in as goals);
  * run_plan: a failed `ask` is NOT retried (re-asking the same question
    twice in a row is harassment, not robustness).

Run: python3 test/test_chores.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

import actions as actions_mod  # noqa: E402
import daemon as daemon_mod  # noqa: E402
import prompts as prompts_mod  # noqa: E402

PLAYER = "Deviant46n2"


class FakeBridge:
    """chat_log serves scripted entries; getblock reads a block map;
    everything else succeeds."""

    def __init__(self, chat=(), inject=None, blocks=None):
        self.calls = []
        self.chat = list(chat)
        self.inject = inject  # (after_polls, message) — player reply arrives late
        self.blocks = dict(blocks or {})
        self.slots = []  # inventory_slots entries: {"slot","item","selected"}
        self.screen = None  # state["open_screen"]
        self.container = None  # container op result
        self.hand_item = None  # hand op reports this
        self.open_ok = False  # press(use) opens the Chest screen
        self.aim_gated = False  # ...but only when aimed at good_aim
        self.good_aim = None
        self.last_look = None
        self._polls = 0

    def call(self, op, **kw):
        self.calls.append((op, kw))
        if op == "chat_log":
            self._polls += 1
            if self.inject and self._polls >= self.inject[0]:
                at, msg = self.inject
                if not any(e["message"] == msg for e in self.chat):
                    n = (self.chat[-1]["n"] if self.chat else 0) + 1
                    self.chat.append({"n": n, "t": 0.0, "message": msg})
            since = kw.get("since", 0)
            latest = self.chat[-1]["n"] if self.chat else since
            return {"entries": [e for e in self.chat if e["n"] > since],
                    "latest": latest}
        if op == "state":
            return {"inventory": {}, "position": {},
                    "open_screen": self.screen}
        if op == "inventory_slots":
            return list(self.slots)
        if op == "container":
            # a screen only exposes slots when it actually opened one
            return self.container if self.screen == "Chest" else None
        if op == "look_at":
            self.last_look = (kw["x"], kw["y"], kw["z"])
            return True
        if op == "close_screen":
            self.screen = None
            return {"closed": True}
        if op == "hand":
            return {"item": self.hand_item, "count": 1}
        if op == "press":
            if (kw.get("control") == "use" and kw.get("pressed")
                    and self.open_ok and self.screen is None
                    and (not self.aim_gated or self._aim_ok())):
                self.screen = "Chest"
            return True
        if op == "getblock":
            return self.blocks.get((kw["x"], kw["y"], kw["z"]), "minecraft:air")
        if op == "targeted_block":
            return {"block": "minecraft:stone", "position": [1, 2, 3]}
        return {"ok": True}

    def _aim_ok(self):
        if self.last_look is None or self.good_aim is None:
            return False
        return all(abs(a - b) < 1e-9
                   for a, b in zip(self.last_look, self.good_aim))


def chats(bridge):
    return [kw["text"] for op, kw in bridge.calls if op == "chat"]


def make_actions(bridge):
    return actions_mod.Actions(bridge, lambda q: set(), lambda *a, **k: None,
                               body="player", player_name=PLAYER)


def test_only_the_players_own_lines_steer_the_bot():
    entries = [
        {"n": 1, "t": 0.0, "message": f"<{PLAYER}> put sand in the crusher"},
        {"n": 2, "t": 0.0, "message": "<CoopBot> [agent] working on: X"},
        {"n": 3, "t": 0.0, "message": "[System] [CHAT] Deviant46n2 fell out of the world"},
        {"n": 4, "t": 0.0, "message": f"<{PLAYER}> [agent] quoting you back"},
        {"n": 5, "t": 0.0, "message": f"<{PLAYER}> and then take the dust out"},
    ]
    out = daemon_mod.extract_player_lines(entries, PLAYER)
    assert out == [(PLAYER, "put sand in the crusher"),
                   (PLAYER, "and then take the dust out")], out


def test_ask_broadcasts_and_hands_back_the_reply():
    bridge = FakeBridge(inject=(2, f"<{PLAYER}> the crusher is at 100 64 200"))
    act = make_actions(bridge)
    ok, note = act.ask({"question": "where is the crusher?", "timeout": 30})
    assert ok is True, f"a reply must complete the step, got {ok!r} {note!r}"
    assert "100 64 200" in note, f"the answer must travel with the note: {note!r}"
    said = chats(bridge)
    assert any("where is the crusher?" in t for t in said), (
        f"the question must be broadcast, got {said}")


def test_ask_repeats_once_then_gives_up():
    bridge = FakeBridge()
    act = make_actions(bridge)
    ok, note = act.ask({"question": "where is the crusher?", "timeout": 0})
    assert ok is False, f"silence must fail the step, got {ok!r}"
    said = [t for t in chats(bridge) if "where is the crusher?" in t]
    assert len(said) == 2, f"ask once + repeat once, got {said}"


def test_chore_prompt_carries_words_not_quests():
    history = [("player", "put sand in the crusher"),
               ("bot", "which crusher — coords?")]
    prompt = prompts_mod.build_chore_prompt(
        "the big one at 100 64 200", history,
        {"inventory": {"sand": 64}, "position": {}})
    assert "the big one at 100 64 200" in prompt, prompt[:500]
    assert "put sand in the crusher" in prompt, prompt[:500]
    assert "Frontier" not in prompt and "target_quests" not in prompt, (
        "quests must not leak into chore planning")


def test_chore_system_knows_ask_and_ignores_quests():
    system = prompts_mod.get_chore_system("player")
    assert '"ask"' in system, "ask must be in the chore vocabulary"
    assert "quest" in system.lower(), "the no-quests rule must be stated"


class FakeActions:
    def __init__(self):
        self.calls = []

    def run(self, step):
        self.calls.append(step["action"]["action"])
        return False, "nope"


def test_run_plan_retries_work_but_never_repeats_a_question():
    plan = {"steps": [{"n": 1, "action": {"action": "ask", "question": "q?"}}]}
    fa = FakeActions()
    done, failed, notes = daemon_mod.run_plan(None, None, plan, fa)
    assert fa.calls == ["ask"], f"ask must run exactly once, got {fa.calls}"

    plan2 = {"steps": [{"n": 1,
                        "action": {"action": "goto", "x": 1, "y": 2, "z": 3}}]}
    fa2 = FakeActions()
    done, failed, notes = daemon_mod.run_plan(None, None, plan2, fa2)
    assert fa2.calls == ["goto", "goto"], (
        f"ordinary steps keep their one retry, got {fa2.calls}")


class RecordingActions(FakeActions):
    def __init__(self):
        super().__init__()
        self.steps = []

    def run(self, step):
        self.calls.append(step["action"]["action"]
                          if isinstance(step.get("action"), dict) else step)
        self.steps.append(step)
        return True, "ok"


def test_run_plan_normalizes_string_actions_instead_of_dying():
    """The local model sometimes emits {"n":1, "action":"take_from_chest",
    "item":"sand", ...} — params beside the action instead of inside it.
    That must run, not kill the daemon."""
    plan = {"steps": [{"n": 1, "action": "take_from_chest",
                       "item": "sand", "count": 8,
                       "at": {"x": 10, "y": 66, "z": 7}}]}
    fa = RecordingActions()
    done, failed, notes = daemon_mod.run_plan(None, None, plan, fa)
    assert failed is None, f"normalized step must run clean, got {failed!r}"
    assert done == 1, done
    action = fa.steps[0]["action"]
    assert action == {"action": "take_from_chest", "item": "sand",
                      "count": 8, "at": {"x": 10, "y": 66, "z": 7}}, action


def test_run_plan_reports_garbage_steps_instead_of_dying():
    plan = {"steps": ["just do the thing"]}
    fa = RecordingActions()
    done, failed, notes = daemon_mod.run_plan(None, None, plan, fa)
    assert done == 0, done
    assert isinstance(failed, dict) and failed.get("n") == "?", failed
    assert fa.calls == [], f"nothing executable must run, got {fa.calls}"


def test_at_coordinates_are_floored_to_block_aim():
    """look_at aims at block centers (x+0.5); a fractional "player position"
    as `at` aims into the next block over and the ray misses."""
    bridge = FakeBridge()
    act = actions_mod.Actions(bridge, lambda q: set(), lambda *a, **k: None,
                              body="player", player_name=PLAYER)
    assert act._block_pos({"x": 9.3, "y": 66.0, "z": 8.4}) == \
        {"x": 9, "y": 66, "z": 8}
    ok, note = act.interact({"block": "transmutation_table",
                             "at": {"x": 9.3, "y": 66.0, "z": 8.4}})
    assert ok is True, (ok, note)
    looks = [kw for op, kw in bridge.calls if op == "look_at"]
    assert looks and looks[0]["x"] == 9.5 and looks[0]["z"] == 8.5, looks


class ScriptedActions(FakeActions):
    """run() replays scripted (ok, note) outcomes in order."""

    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)

    def run(self, step):
        action = step.get("action", {})
        self.calls.append(action.get("action"))
        ok, note = self.outcomes.pop(0) if self.outcomes else (True, "ok")
        return ok, note


def test_run_plan_reports_what_each_step_did():
    """The next turn plans blind unless it sees WHY the last one stopped —
    notes travel back into the conversation, not just the log file."""
    plan = {"steps": [{"n": 1, "action": {"action": "goto", "x": 1, "y": 2, "z": 3}},
                      {"n": 2, "action": {"action": "interact",
                                          "block": "table", "gui": True}}]}
    fa = ScriptedActions([(True, "arrived"), (False, "screen: None"),
                            (False, "screen: None (retry)")])
    done, failed, notes = daemon_mod.run_plan(None, None, plan, fa)
    assert done == 1, (done, notes)
    assert failed["n"] == 2, failed
    assert [n["ok"] for n in notes] == [True, False], notes
    assert notes[1]["note"] == "screen: None (retry)", notes
    assert notes[0]["action"] == "goto", notes


def test_inspect_reports_the_block_at_floored_coords():
    bridge = FakeBridge(blocks={(9, 65, 8): "kubejs:transmutation_table"})
    act = make_actions(bridge)
    ok, note = act.inspect({"at": {"x": 9.3, "y": 65.9, "z": 8.1}})
    assert ok is True, (ok, note)
    assert "kubejs:transmutation_table" in note, note
    asked = [(kw["x"], kw["y"], kw["z"]) for op, kw in bridge.calls
             if op == "getblock"]
    assert asked == [(9, 65, 8)], f"must ask about whole blocks, got {asked}"


def test_inspect_without_coords_reports_what_is_looked_at():
    bridge = FakeBridge()
    act = make_actions(bridge)
    ok, note = act.inspect({})
    assert ok is True, (ok, note)
    assert "minecraft:stone" in note, note


def test_long_wait_aborts_when_the_player_talks():
    bridge = FakeBridge(inject=(2, f"<{PLAYER}> stop that, come here"))
    act = make_actions(bridge)
    assert act._wait(lambda: False, timeout=30, poll=0.01) is False
    assert act.interrupted and act.interrupted[0][1] == "stop that, come here", \
        act.interrupted


def test_wait_ignores_own_lines_and_server_noise():
    bridge = FakeBridge(inject=(1, "<CoopBot> [agent] working on: X"))
    act = make_actions(bridge)
    assert act._wait(lambda: False, timeout=0.05, poll=0.01) is False
    assert act.interrupted == [], act.interrupted


def test_goto_stops_moving_and_reports_the_interruption():
    bridge = FakeBridge(inject=(3, f"<{PLAYER}> never mind, stay"))
    act = make_actions(bridge)
    ok, note = act.goto({"x": 100, "y": 64, "z": 200, "timeout": 60})
    assert ok is False, (ok, note)
    assert note.startswith("interrupted:"), note
    assert "never mind, stay" in note, note
    stops = [kw["text"] for op, kw in bridge.calls
             if op == "chat" and kw.get("text") == "#stop"]
    assert stops, f"baritone must be halted on interrupt: {bridge.calls}"


def test_run_plan_does_not_retry_an_interrupted_step():
    plan = {"steps": [{"n": 1,
                       "action": {"action": "goto", "x": 1, "y": 2, "z": 3}}]}
    fa = FakeActions()
    fa.interrupted = [(PLAYER, "stop that")]
    done, failed, notes = daemon_mod.run_plan(None, None, plan, fa)
    assert fa.calls == ["goto"], f"interrupted steps run once, got {fa.calls}"
    assert done == 0 and failed is not None, (done, failed)


def test_opening_blocks_starts_with_an_empty_hand():
    """Right-clicking with a usable item held fires the ITEM when the ray
    misses the block — observed live: aiming up at an elevated chest
    grazed its pillar and opened 'Materials and You' instead."""
    bridge = FakeBridge()
    bridge.slots = [{"slot": 0, "item": "guide_book", "count": 1,
                     "selected": True},
                    {"slot": 9, "item": "sand", "count": 64,
                     "selected": False}]
    act = make_actions(bridge)
    assert act._safe_hand() is True
    selected = [kw["slot"] for op, kw in bridge.calls if op == "select_slot"]
    assert selected and selected[0] not in (0,), (
        f"must move off the book onto an empty hotbar slot, got {selected}")


def test_empty_hand_already_is_left_alone():
    bridge = FakeBridge()
    bridge.slots = [{"slot": 9, "item": "sand", "count": 64,
                     "selected": False}]
    act = make_actions(bridge)
    assert act._safe_hand() is True
    assert not [1 for op, _ in bridge.calls if op == "select_slot"], (
        f"no need to switch when nothing usable is held: {bridge.calls}")


def test_open_failure_names_what_the_ray_hits():
    bridge = FakeBridge()  # targeted_block -> stone, screen stays shut
    act = make_actions(bridge)
    reason = act._open_container({"x": 5, "y": 69, "z": 0})
    assert reason is not None, "stone pillar must not open anything"
    assert "stone" in reason, f"the note must name the blocker: {reason!r}"


def test_wrong_screen_names_aim_and_screen():
    bridge = FakeBridge()
    bridge.screen = "Materials and You"  # the held-book misfire, live case
    act = make_actions(bridge)
    reason = act._open_container({"x": 5, "y": 69, "z": 0})
    assert reason is not None, reason
    assert "Materials and You" in reason and "stone" in reason, (
        f"planner needs both halves of the diagnosis: {reason!r}")


def test_open_searches_aim_points_until_the_chest_opens():
    """Center-aim can graze an occluder below an elevated chest while a
    bottom-corner ray slips past it — keep aiming until slots appear."""
    bridge = FakeBridge()
    bridge.open_ok = True
    bridge.aim_gated = True
    bridge.good_aim = (5.9, 69.05, 0.9)  # a bottom corner, not the center
    bridge.container = []
    act = make_actions(bridge)
    assert act._open_container({"x": 5, "y": 69, "z": 0}) is None
    looks = [kw for op, kw in bridge.calls if op == "look_at"]
    assert len(looks) >= 2, f"must try past the blocked center: {looks}"


def test_open_reports_every_aim_when_nothing_opens():
    bridge = FakeBridge()
    bridge.open_ok = True
    bridge.aim_gated = True
    bridge.good_aim = (999, 999, 999)  # unreachable: everything misses
    act = make_actions(bridge)
    reason = act._open_container({"x": 5, "y": 69, "z": 0})
    assert reason is not None and "5,69,0" in reason, reason
    looks = [kw for op, kw in bridge.calls if op == "look_at"]
    assert len(looks) >= 3, f"must work the aim points, got {looks}"


def test_select_item_closes_a_stuck_screen_first():
    bridge = FakeBridge()
    bridge.screen = "Materials and You"  # left open by a book misfire
    bridge.slots = [{"slot": 7, "item": "sand", "count": 64,
                     "selected": False}]
    bridge.hand_item = "sand"
    act = make_actions(bridge)
    assert act._select_item("sand") is True
    ops = [op for op, _ in bridge.calls]
    assert "close_screen" in ops and "select_slot" in ops, ops
    assert ops.index("close_screen") < ops.index("select_slot"), ops
    assert bridge.screen is None, "stuck GUI must be gone"


def test_select_distinguishes_missing_from_unselectable():
    bridge = FakeBridge()
    act = make_actions(bridge)
    assert act._select_item("sand") == "missing"
    bridge.slots = [{"slot": 7, "item": "sand", "count": 64,
                     "selected": False}]
    bridge.hand_item = "materials_and_you"  # selection never lands
    assert act._select_item("sand") == "select_failed"


def test_manual_completes_on_any_player_reply():
    """manual is a conversation, not a .done gate: any reply continues the
    turn (the reply text travels, so the next plan knows what was said).
    A misfired escalation must never freeze the bot for 15 minutes."""
    bridge = FakeBridge(inject=(2, f"<{PLAYER}> give me a minute"))
    act = make_actions(bridge)
    ok, note = act.manual({"reason": "drive the boat", "timeout": 30})
    assert ok is True, (ok, note)
    assert "give me a minute" in note, note
    said = chats(bridge)
    assert any("NEED HELP" in t for t in said), said


def test_manual_fails_on_silence_not_on_chatter():
    bridge = FakeBridge()
    act = make_actions(bridge)
    ok, note = act.manual({"reason": "drive the boat", "timeout": 0})
    assert ok is False, (ok, note)


if __name__ == "__main__":
    test_only_the_players_own_lines_steer_the_bot()
    print("PASS test_only_the_players_own_lines_steer_the_bot")
    test_ask_broadcasts_and_hands_back_the_reply()
    print("PASS test_ask_broadcasts_and_hands_back_the_reply")
    test_ask_repeats_once_then_gives_up()
    print("PASS test_ask_repeats_once_then_gives_up")
    test_chore_prompt_carries_words_not_quests()
    print("PASS test_chore_prompt_carries_words_not_quests")
    test_chore_system_knows_ask_and_ignores_quests()
    print("PASS test_chore_system_knows_ask_and_ignores_quests")
    test_run_plan_retries_work_but_never_repeats_a_question()
    print("PASS test_run_plan_retries_work_but_never_repeats_a_question")
    test_run_plan_normalizes_string_actions_instead_of_dying()
    print("PASS test_run_plan_normalizes_string_actions_instead_of_dying")
    test_run_plan_reports_garbage_steps_instead_of_dying()
    print("PASS test_run_plan_reports_garbage_steps_instead_of_dying")
    test_at_coordinates_are_floored_to_block_aim()
    print("PASS test_at_coordinates_are_floored_to_block_aim")
    test_run_plan_reports_what_each_step_did()
    print("PASS test_run_plan_reports_what_each_step_did")
    test_inspect_reports_the_block_at_floored_coords()
    print("PASS test_inspect_reports_the_block_at_floored_coords")
    test_inspect_without_coords_reports_what_is_looked_at()
    print("PASS test_inspect_without_coords_reports_what_is_looked_at")
    test_long_wait_aborts_when_the_player_talks()
    print("PASS test_long_wait_aborts_when_the_player_talks")
    test_wait_ignores_own_lines_and_server_noise()
    print("PASS test_wait_ignores_own_lines_and_server_noise")
    test_goto_stops_moving_and_reports_the_interruption()
    print("PASS test_goto_stops_moving_and_reports_the_interruption")
    test_run_plan_does_not_retry_an_interrupted_step()
    print("PASS test_run_plan_does_not_retry_an_interrupted_step")
    test_opening_blocks_starts_with_an_empty_hand()
    print("PASS test_opening_blocks_starts_with_an_empty_hand")
    test_empty_hand_already_is_left_alone()
    print("PASS test_empty_hand_already_is_left_alone")
    test_open_failure_names_what_the_ray_hits()
    print("PASS test_open_failure_names_what_the_ray_hits")
    test_wrong_screen_names_aim_and_screen()
    print("PASS test_wrong_screen_names_aim_and_screen")
    test_open_searches_aim_points_until_the_chest_opens()
    print("PASS test_open_searches_aim_points_until_the_chest_opens")
    test_open_reports_every_aim_when_nothing_opens()
    print("PASS test_open_reports_every_aim_when_nothing_opens")
    test_select_item_closes_a_stuck_screen_first()
    print("PASS test_select_item_closes_a_stuck_screen_first")
    test_select_distinguishes_missing_from_unselectable()
    print("PASS test_select_distinguishes_missing_from_unselectable")
    test_manual_completes_on_any_player_reply()
    print("PASS test_manual_completes_on_any_player_reply")
    test_manual_fails_on_silence_not_on_chatter()
    print("PASS test_manual_fails_on_silence_not_on_chatter")
    print("PASS")
