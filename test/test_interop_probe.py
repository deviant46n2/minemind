"""op_interop_probe: walk the container-click interop chain, read-only.

Why this exists: container writing needs
`AbstractContainerMenu.m_150399_(slot, button, ClickType, player)`, which needs
live object handles for `Minecraft.player` and `Player.containerMenu`. Both are
OBJECT fields, and op_close_screen's hard-won lesson (see its docstring) is that
minescript's java_access_field hands back a truthy null-ish handle for those.
Nothing else in the repo reads an object field through interop, so the whole
container feature rests on one unverified assumption.

This op is the experiment. It must:
  * report per-step, so the exact break point is visible;
  * never raise out of a bad handle (that is the failure we are hunting);
  * never mutate anything — no clicked(), no setScreen(), nothing.

SRG names are taken from javap against
forge-1.20.1-47.4.16-client.jar + mcp_config mappings, not remembered:
  Minecraft.getInstance      m_91087_
  Minecraft.player           f_91074_
  Player.containerMenu       f_36096_
  AbstractContainerMenu.getType  m_6772_
  AbstractContainerMenu.clicked  m_150399_
  ClickType.PICKUP           PICKUP   (enum constants keep their names)

Run: python3 test/test_interop_probe.py
"""
import importlib.util
import sys
import types
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "minescript" / "agent_bridge.py"


class Nullish:
    """A handle that is truthy in Python but null in Java — minescript's bug."""

    def __bool__(self):
        return True


def load_bridge(work=True, break_player_field=False):
    """fake minescript module modelling the java_* interop.

    work=False        -> java_* functions absent entirely
    break_player_field -> f_91074_ read returns a Nullish handle that dies on use
    """
    calls = []

    def java_class(name):
        calls.append(("java_class", name))
        return ("cls", name)

    def java_member(cls, name):
        calls.append(("java_member", cls[1], name))
        return ("mem", cls[1], name)

    def java_call_method(target, method, *args):
        calls.append(("java_call_method", method[2] if len(method) > 2 else method, args))
        if target is None:
            raise TypeError("Cannot invoke because obj is null")
        if isinstance(target, Nullish):
            raise TypeError("Cannot invoke Object.getClass() because obj is null")
        return ("obj", f"{target[1]}.{method[2]}")

    def java_access_field(target, field):
        calls.append(("java_access_field", field[2]))
        if isinstance(target, Nullish) or target is None:
            raise TypeError("Cannot access field because obj is null")
        if break_player_field and field[2] == "f_91074_":
            return Nullish()
        return ("obj", field[2])

    def java_to_string(target):
        calls.append(("java_to_string", target))
        if isinstance(target, Nullish):
            raise TypeError("Cannot invoke Object.toString() because obj is null")
        return f"<{target[1]}>"

    fake = types.ModuleType("minescript")
    if work:
        fake.java_class = java_class
        fake.java_member = java_member
        fake.java_call_method = java_call_method
        fake.java_access_field = java_access_field
        fake.java_to_string = java_to_string
    sys.modules["minescript"] = fake

    spec = importlib.util.spec_from_file_location("agent_bridge_interop_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, calls


def step_names(out):
    return [s["name"] for s in out.get("steps", [])]


def test_probe_walks_the_real_chain():
    mod, calls = load_bridge()
    out = mod.op_interop_probe({})
    assert out["ok"] is True, f"expected ok, got {out!r}"
    names = step_names(out)
    assert "Minecraft.getInstance" in names, names
    assert "Minecraft.player" in names, names
    assert "Player.containerMenu" in names, names
    assert "ClickType.PICKUP" in names, names
    # the SRG names must be the javap-verified ones, not remembered guesses
    used = [c[2] for c in calls if c[0] == "java_member"]
    for srg in ("m_91087_", "f_91074_", "f_36096_", "m_6772_"):
        assert srg in used, f"{srg} missing from {used}"


def test_probe_survives_truthy_null_object_field():
    """The live bug shape: field reads hand back a handle Java sees as null.

    The op must degrade to a reported per-step failure — it is the diagnostic
    for this exact situation, so it must not be killed by it.
    """
    mod, calls = load_bridge(break_player_field=True)
    out = mod.op_interop_probe({})
    assert out["ok"] is False, f"broken field read must not report ok, got {out!r}"
    failed = [s for s in out["steps"] if not s["ok"]]
    assert failed, f"expected a failed step, got {out!r}"
    assert "null" in failed[0]["detail"].lower(), f"got {failed[0]!r}"
    assert "Minecraft.player" in [s["name"] for s in failed], out
    # steps after the break must be absent, not silently reported as ok
    names = step_names(out)
    assert "Player.containerMenu" not in names, names


def test_probe_reports_missing_interop():
    mod, _ = load_bridge(work=False)
    out = mod.op_interop_probe({})
    assert out["ok"] is False, out
    assert "java_access_field" in out["missing"], out
    assert not out.get("steps"), out


def test_probe_is_read_only():
    """clicked() (m_150399_) and setScreen (m_91152_) must never fire."""
    mod, calls = load_bridge()
    mod.op_interop_probe({})
    called = [c[1] for c in calls if c[0] == "java_call_method"]
    assert "m_150399_" not in called, called
    assert "m_91152_" not in called, called


def test_probe_wired_into_ops():
    mod, _ = load_bridge()
    assert mod.OPS.get("interop_probe") is mod.op_interop_probe, (
        "OPS must expose interop_probe so the daemon can call it")


if __name__ == "__main__":
    for fn in (test_probe_walks_the_real_chain,
               test_probe_survives_truthy_null_object_field,
               test_probe_reports_missing_interop,
               test_probe_is_read_only,
               test_probe_wired_into_ops):
        fn()
        print(f"PASS {fn.__name__}")
    print("PASS")
