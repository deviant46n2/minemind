"""op_container_click — the only path that WRITES to an open container.

Why interop instead of the documented API: minescript ships no
container_click_slot (verified by unzipping minescript-forge-1.20.1-4.0.jar),
which is why agent_take/agent_store in `--body player` have never worked —
the old op forwarded to a function that does not exist and silently returned
None. The live GUI click is vanilla's

    MultiPlayerGameMode.handleInventoryMouseClick(
        int containerId, int slot, int button, ClickType, Player)
    SRG m_171799_

which needs live handles for Minecraft.player (f_91074_),
Minecraft.gameMode (f_91072_) and Player.containerMenu (f_36096_).

Why not AbstractContainerMenu.clicked (m_150399_) directly — the first
version did, and it silently did nothing durable. clicked() mutates the
client-side menu only; vanilla reaches it through
AbstractContainerScreen.slotClicked -> gameMode.handleInventoryMouseClick,
which ALSO sends ServerboundContainerClickPacket. Without that packet the
integrated server never hears about the move: the GUI shows the transfer
while open, then the next resync snaps every item back (observed live —
/close a chest after two QUICK_MOVEs and `data get block` reports Items: []).

Invariants this test pins down, because each one is a way it could silently
corrupt the player's inventory:

  * it must refuse to click when no container GUI is open — a bare
    player.containerMenu is always non-null (the InventoryMenu), so without
    this check "click the chest" would quietly shuffle the player's own slots;
  * containerId must be the value the field holds, not the object-table handle
    minescript wraps it in — every java_* result is `job.objects.retain(...)`,
    so a raw Python int from java_access_field IS a handle. Sending the handle
    as the container id makes vanilla drop the click on the floor (live:
    "Ignoring click in mismatching container. Click in 13, player has 1.");
  * args must be boxed with java_int, not passed as raw Python ints;
  * click_type is a whitelist, not an arbitrary enum name;
  * a broken object-field handle must degrade to a reported failure, never an
    exception out of the op (the exact shape op_close_screen hit live);
  * SRG names must be the javap-verified ones, and the call must go through
    gameMode.handleInventoryMouseClick, never straight to menu.clicked() —
    the latter is the client-only no-op that lost every test transfer.

Run: python3 test/test_container_click.py
"""
import importlib.util
import sys
import types
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "minescript" / "agent_bridge.py"

# What AbstractContainerMenu.containerId holds, and the object-table handle
# minescript wraps it in (java_access_field -> job.objects.retain).
CID_VALUE, CID_HANDLE = 1, 91


class Nullish:
    """Truthy in Python, null in Java — minescript's java_access_field bug."""

    def __bool__(self):
        return True


def load_bridge(screen="Chest", break_player_field=False, work=True):
    calls = []

    def java_class(name):
        calls.append(("java_class", name))
        return ("cls", name)

    def java_member(cls, name):
        calls.append(("java_member", name))
        return ("mem", name)

    def java_int(i):
        calls.append(("java_int", i))
        return ("int", i)

    def java_to_string(handle):
        calls.append(("java_to_string", handle))
        if handle == CID_HANDLE:
            return str(CID_VALUE)
        if isinstance(handle, Nullish):
            return "null"
        return str(handle)

    def java_call_method(target, method, *args):
        name = method[1] if len(method) > 1 else method
        calls.append(("java_call_method", name, args))
        if target is None or isinstance(target, Nullish):
            raise TypeError("Cannot invoke Object.getClass() because obj is null")
        if name == "m_91087_":
            return ("mc",)
        if name in ("m_150399_", "m_171799_"):
            return None
        return ("obj", name)

    def java_access_field(target, field):
        name = field[1] if len(field) > 1 else field
        calls.append(("java_access_field", name))
        if target is None or isinstance(target, Nullish):
            raise TypeError("Cannot access field because obj is null")
        if break_player_field and name == "f_91074_":
            return Nullish()
        if name == "PICKUP":
            return ("enum", "PICKUP")
        if name == "f_38840_":          # AbstractContainerMenu.containerId: int
            return CID_HANDLE           # a handle, not the int the field holds
        return ("obj", name)

    fake = types.ModuleType("minescript")
    if work:
        fake.java_class = java_class
        fake.java_member = java_member
        fake.java_call_method = java_call_method
        fake.java_access_field = java_access_field
        fake.java_int = java_int
        fake.java_to_string = java_to_string
        fake.screen_name = lambda: screen
    sys.modules["minescript"] = fake

    spec = importlib.util.spec_from_file_location("agent_bridge_click_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, calls


def clicked_calls(calls):
    """The server-notifying click path (handleInventoryMouseClick)."""
    return [c for c in calls if c[0] == "java_call_method" and c[1] == "m_171799_"]


def legacy_calls(calls):
    """A bare menu.clicked() — client-side only, so it must never appear."""
    return [c for c in calls if c[0] == "java_call_method" and c[1] == "m_150399_"]


def members(calls):
    return [c[1] for c in calls if c[0] == "java_member"]


def test_click_walks_the_verified_chain():
    mod, calls = load_bridge()
    out = mod.op_container_click({"slot": 3, "button": 0, "click_type": "PICKUP"})
    assert out.get("ok") is True, f"expected ok, got {out!r}"
    # SRG names from javap against forge-1.20.1-47.4.16-client.jar
    used = members(calls)
    for srg in ("m_91087_", "f_91074_", "f_91072_", "f_36096_",
                "f_38840_", "m_171799_"):
        assert srg in used, f"{srg} missing from {used}"
    assert ("java_class", "net.minecraft.client.Minecraft") in calls, calls
    assert ("java_access_field", "PICKUP") in calls, (
        "ClickType.PICKUP is an enum constant, reached via a static field read", calls)
    assert ("java_class", "net.minecraft.client.multiplayer.MultiPlayerGameMode") in calls, calls
    hits = clicked_calls(calls)
    assert len(hits) == 1, f"must call handleInventoryMouseClick once, got {hits}"
    assert not legacy_calls(calls), (
        "menu.clicked() alone never reaches the server: the GUI reverts on the "
        f"next resync. It must not be called: {legacy_calls(calls)}")


def test_click_boxes_args_with_java_int():
    """Raw Python ints would reach Method.invoke unboxed and die there."""
    mod, calls = load_bridge()
    mod.op_container_click({"slot": 3, "button": 2, "click_type": "PICKUP"})
    boxed = [c[1] for c in calls if c[0] == "java_int"]
    assert boxed == [CID_VALUE, 3, 2], (
        f"containerId, slot and button must all be java_int-boxed, got {boxed}")
    hits = clicked_calls(calls)
    assert hits, f"no click issued: {calls}"
    args = hits[0][2]
    assert args[:3] == (("int", CID_VALUE), ("int", 3), ("int", 2)), (
        f"call must be (containerId, slot, button, ...), got {args!r}")
    assert args[3] == ("enum", "PICKUP"), f"got {args!r}"
    assert args[4] == ("obj", "f_91074_"), (
        "the last arg is the Player itself (the same f_91074_ handle)", args)


def test_click_sends_the_container_id_value_not_its_handle():
    """Every java_* result is job.objects.retain(...) — an object-table id.

    Feeding that id to handleInventoryMouseClick makes vanilla discard the
    click without touching anything (live, twice):

        [MultiPlayerGameMode] Ignoring click in mismatching container.
        Click in 13, player has 1.

    so the field's value has to be unboxed with java_to_string first.
    """
    mod, calls = load_bridge()
    out = mod.op_container_click({"slot": 3, "button": 0, "click_type": "PICKUP"})
    assert out.get("ok") is True, out
    hits = clicked_calls(calls)
    assert hits, f"no click issued: {calls}"
    assert hits[0][2][0] == ("int", CID_VALUE), (
        f"containerId sent was {hits[0][2][0]}, expected the value {CID_VALUE} "
        f"rather than handle {CID_HANDLE}")
    assert ("java_to_string", CID_HANDLE) in calls, (
        f"the handle must be read through java_to_string: {calls}")


def test_click_refuses_without_an_open_container():
    """A bare containerMenu is always non-null (InventoryMenu) — clicking
    anyway would shuffle the player's own inventory while claiming to have
    touched the chest."""
    mod, calls = load_bridge(screen=None)
    out = mod.op_container_click({"slot": 0})
    assert out.get("ok") is False, f"no screen must not be ok, got {out!r}"
    assert "container" in (out.get("reason") or ""), f"got {out!r}"
    assert not clicked_calls(calls), f"must not call clicked(), got {calls}"


def test_click_rejects_unknown_click_type():
    mod, calls = load_bridge()
    out = mod.op_container_click({"slot": 0, "click_type": "valueOf"})
    assert out.get("ok") is False, out
    assert not clicked_calls(calls), calls


def test_click_degrades_on_broken_object_field():
    """op_close_screen's live failure mode: java_access_field hands back a
    handle Java sees as null. The op must report it, never raise it."""
    mod, calls = load_bridge(break_player_field=True)
    out = mod.op_container_click({"slot": 0})
    assert out.get("ok") is False, f"broken field must not report ok, got {out!r}"
    assert out.get("reason"), out
    assert not clicked_calls(calls), calls


def test_click_never_raises_when_interop_is_absent():
    mod, calls = load_bridge(work=False)
    out = mod.op_container_click({"slot": 0})
    assert out.get("ok") is False, out
    assert out.get("reason"), out


def test_container_exposes_slot():
    """container_get_items' ItemStack carries `slot`; without it a consumer
    has no address to click at, so op_container must forward it."""
    mod, calls = load_bridge()

    def container_get_items():
        return [{"item": "sand", "count": 64, "slot": 11},
                {"item": "lava_bucket", "count": 1, "slot": 4}]

    sys.modules["minescript"].container_get_items = container_get_items
    out = mod.op_container({})
    assert out == [{"item": "sand", "count": 64, "slot": 11},
                   {"item": "lava_bucket", "count": 1, "slot": 4}], out


def test_container_click_wired_into_ops():
    mod, _ = load_bridge()
    assert mod.OPS.get("container_click") is mod.op_container_click, (
        "OPS must expose container_click so the daemon can call it")


if __name__ == "__main__":
    for fn in (test_click_walks_the_verified_chain,
               test_click_boxes_args_with_java_int,
               test_click_sends_the_container_id_value_not_its_handle,
               test_click_refuses_without_an_open_container,
               test_click_rejects_unknown_click_type,
               test_click_degrades_on_broken_object_field,
               test_click_never_raises_when_interop_is_absent,
               test_container_exposes_slot,
               test_container_click_wired_into_ops):
        fn()
        print(f"PASS {fn.__name__}")
    print("PASS")
