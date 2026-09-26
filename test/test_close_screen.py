"""op_close_screen must dismiss the bot's GUI (Game Menu) in-process.

Design facts, all proven live:

1. Screen.onClose() (SRG m_7379_) is the wrong action: PauseScreen does not
   override it, so it runs Screen.onClose() -> Minecraft.popGuiLayer() and
   the menu stays up. Vanilla's "Back to Game" button calls
   Minecraft.setScreen(null) (SRG m_91152_), so that is the op.

2. Minecraft.f_91080_ (the screen field) cannot be read through minescript's
   interop: java_access_field returns a truthy null-ish handle for an object
   field. That is what made the old op die with
   NullPointerException ("Cannot invoke Object.getClass()" because obj is
   null), and what made an earlier revision report "still open" while the
   screen was in fact gone.

3. screen_name() is the observable the rest of the system trusts (op_state
   fills open_screen from it), so close_screen verifies with it -- then
   close_screen and state can never disagree.

Why in-process at all: any OS-level Escape/click steals focus from the
human's window, and focus loss is what opens the Game Menu in the first
place (Options.pauseOnLostFocus -> GameRenderer -> PauseScreen).
"""
import importlib.util
import sys
import types
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "minescript" / "agent_bridge.py"


def load_bridge(initial_screen="Game Menu", set_screen_clears=True, with_screen_name=True):
    calls = []
    world = {"name": initial_screen}

    fake = types.ModuleType("minescript")
    fake.java_null = 0

    def java_class(name):
        calls.append(("java_class", name))
        return ("cls", name)

    def java_member(cls, name):
        calls.append(("java_member", name))
        return ("member", name)

    def java_call_method(target, method, *args):
        calls.append(("java_call_method", method[1], target, args))
        if method[1] == "m_91152_" and set_screen_clears:
            world["name"] = None  # Minecraft.setScreen(null) cleared it
        return None

    def java_access_field(target, field):
        # Deliberately unhelpful, like the real thing: minescript hands back a
        # truthy null-ish handle for object fields, so the op must not use it.
        calls.append(("java_access_field", field[1], target))
        return ("null-ish",)

    fake.java_class = java_class
    fake.java_member = java_member
    fake.java_call_method = java_call_method
    fake.java_access_field = java_access_field
    if with_screen_name:
        def screen_name():
            calls.append(("screen_name", world["name"]))
            return world["name"]
        fake.screen_name = screen_name
    sys.modules["minescript"] = fake

    spec = importlib.util.spec_from_file_location("agent_bridge_close_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, calls


def _set_screen_calls(calls):
    return [c for c in calls if c[0] == "java_call_method" and c[1] == "m_91152_"]


def test_close_screen_closes_via_set_screen():
    mod, calls = load_bridge(initial_screen="Game Menu")
    out = mod.op_close_screen({})
    assert out == {"closed": True}, f"expected closed=True, got {out!r}"
    assert ("java_class", "net.minecraft.client.Minecraft") in calls, calls
    # Forge 1.20.1 runtime is SRG-named: getInstance=m_91087_,
    # Minecraft.setScreen=m_91152_.
    assert ("java_call_method", "m_91087_", ("cls", "net.minecraft.client.Minecraft"),
            ()) in calls, calls
    hits = _set_screen_calls(calls)
    assert hits, f"must call Minecraft.setScreen (SRG m_91152_), got {calls}"
    assert hits[0][3] == (0,), (
        f"setScreen must receive minescript's null handle 0, got {hits[0][3]!r}")
    # The screen must be re-checked *after* the call, so a no-op close cannot
    # be reported as success (live bug: closed=True while the menu was up).
    names_at = [i for i, c in enumerate(calls) if c[0] == "screen_name"]
    set_at = [i for i, c in enumerate(calls) if c[0] == "java_call_method" and c[1] == "m_91152_"]
    assert len(names_at) >= 2 and max(names_at) > max(set_at), (
        f"must re-check screen_name() after setScreen, got {calls}")


def test_close_screen_reports_stuck_screen_honestly():
    mod, calls = load_bridge(initial_screen="Game Menu", set_screen_clears=False)
    out = mod.op_close_screen({})
    assert out.get("closed") is False, f"stuck screen must not report closed=True, got {out!r}"
    assert "still open" in (out.get("reason") or ""), f"got {out!r}"
    assert _set_screen_calls(calls), "must still have tried setScreen"


def test_close_screen_no_screen_is_clean_noop():
    mod, calls = load_bridge(initial_screen=None)
    out = mod.op_close_screen({})
    assert out == {"closed": False, "reason": "no screen"}, f"got {out!r}"
    assert not _set_screen_calls(calls), calls


def test_close_screen_ignores_truthy_null_field():
    # Regression: the op must not read Minecraft.f_91080_ at all -- minescript
    # returns a truthy handle for it even when the screen field is null, which
    # used to send a null target into Method.invoke (NPE) and later made the
    # op claim the screen was stuck open.
    mod, calls = load_bridge(initial_screen=None)
    mod.op_close_screen({})
    assert not any(c[0] == "java_access_field" for c in calls), (
        "must not read f_91080_ through interop", calls)


def test_close_screen_wired_into_ops():
    mod, _ = load_bridge()
    assert mod.OPS.get("close_screen") is mod.op_close_screen, (
        "OPS must expose close_screen so the daemon can call it")


if __name__ == "__main__":
    test_close_screen_closes_via_set_screen()
    print("PASS test_close_screen_closes_via_set_screen")
    test_close_screen_reports_stuck_screen_honestly()
    print("PASS test_close_screen_reports_stuck_screen_honestly")
    test_close_screen_no_screen_is_clean_noop()
    print("PASS test_close_screen_no_screen_is_clean_noop")
    test_close_screen_ignores_truthy_null_field()
    print("PASS test_close_screen_ignores_truthy_null_field")
    test_close_screen_wired_into_ops()
    print("PASS test_close_screen_wired_into_ops")
