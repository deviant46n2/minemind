r"""agent_bridge — runs INSIDE Minecraft via Minescript 4.x.

Start from in-game chat:  \agent_bridge
Connects to the agent daemon on localhost:48750 (start daemon first) and
serves JSON-lines requests: {"id": N, "op": "...", ...} -> {"id": N, "ok": true, "result": ...}

Ops: probe, state, chat, echo, chat_log, getblock, stop
Defensive about the Minescript API surface: anything missing is reported by
"probe" and returns null in "state" instead of crashing the bridge.
"""
import json
import os
import socket
import threading
import time
from collections import deque

import minescript  # provided by the Minescript mod's embedded interpreter

HOST, PORT = "127.0.0.1", 48750

CHAT_LOG = deque(maxlen=500)
CHAT_SEQ = {"n": 0}

# net.minecraft.world.inventory.ClickType constants — enum names are not
# obfuscated, but an unvalidated name would be handed straight to a static
# field read, so container_click only accepts these.
CLICK_TYPES = {"PICKUP", "PICKUP_ALL", "QUICK_MOVE", "SWAP", "CLONE", "THROW", "QUICK_CRAFT"}


def _fn(name):
    return getattr(minescript, name, None)


def _item_entry(stack):
    get = (lambda k: stack.get(k)) if isinstance(stack, dict) else (lambda k: getattr(stack, k, None))
    return get("item"), get("count") or 1


def op_probe(_req):
    wanted = ["player_position", "player_orientation", "player_inventory",
              "player_health", "player_hand_items", "player_name", "world_info",
              "chat", "echo", "execute", "getblock", "player_look_at",
              "player_press_forward", "player_press_use", "player_press_attack",
              "player_press_sneak", "player_press_jump",
              "player_inventory_select_slot", "player_inventory_slot_to_hotbar",
              "entities", "player_get_targeted_block",
              "container_get_items", "screen_name", "EventQueue"]
    return {name: bool(_fn(name)) for name in wanted}


def op_state(_req):
    out = {}
    name = _fn("player_name")
    if name:
        try:
            out["name"] = name()
        except Exception:
            pass
    pos = _fn("player_position")
    p = None
    if pos:
        try:
            p = pos()
        except Exception:
            p = None
    if p:
        x, y, z = p
        out["position"] = {"x": round(x, 1), "y": round(y, 1), "z": round(z, 1)}
    health = _fn("player_health")
    if health:
        try:
            out["health"] = health()
        except Exception:
            out["health"] = None
    inv = _fn("player_inventory")
    if inv:
        items = {}
        for stack in inv() or []:
            item_id, count = _item_entry(stack)
            if item_id:
                items[item_id] = items.get(item_id, 0) + int(count)
        out["inventory"] = items
    world_info = _fn("world_info")
    if world_info:
        try:
            info = world_info()
            for key in ("day_ticks", "game_ticks", "raining", "hardcore"):
                val = getattr(info, key, None) if not isinstance(info, dict) else info.get(key)
                if val is not None:
                    out[key] = val
        except Exception:
            pass
    screen = _fn("screen_name")
    if screen:
        try:
            out["open_screen"] = screen()
        except Exception:
            pass
    return out


def op_chat(req):
    text = req["text"]
    # Minescript: chat() sends chat/commands; execute() is command-only on some versions
    if text.startswith("/") and _fn("execute"):
        _fn("execute")(text)
    else:
        _fn("chat")(text)
    return True


def op_echo(req):
    fn = _fn("echo") or _fn("chat")
    fn(str(req["text"]))
    return True


def op_chat_log(req):
    since = req.get("since", 0)
    entries = [e for e in CHAT_LOG if e["n"] > since]
    return {"entries": entries, "latest": CHAT_SEQ["n"]}


def op_getblock(req):
    gb = _fn("getblock")
    return gb(req["x"], req["y"], req["z"]) if gb else None


def op_container(_req):
    """Items in the currently open container GUI (None if nothing open).

    Forwards `slot` too: container_click addresses slots by index, so a
    listing without addresses is unreadable for writing.
    """
    fn = _fn("container_get_items")
    if not fn:
        return None
    items = fn()
    if items is None:
        return None
    out = []
    for stack in items:
        get = (lambda k: stack.get(k)) if isinstance(stack, dict) else (lambda k: getattr(stack, k, None))
        item_id, count = _item_entry(stack)
        if item_id:
            entry = {"item": item_id, "count": int(count)}
            if get("slot") is not None:
                entry["slot"] = int(get("slot"))
            out.append(entry)
    return out


def op_look_at(req):
    _fn("player_look_at")(req["x"], req["y"], req["z"])
    return True


def op_press(req):
    """Hold/release a movement or click control: use, attack, forward, jump, sneak..."""
    fn = _fn("player_press_" + req["control"])
    if not fn:
        return None
    fn(bool(req["pressed"]))
    return True


def op_inventory_slots(_req):
    """Player inventory with slot numbers (0-8 hotbar, 9-35 main)."""
    inv = _fn("player_inventory")
    out = []
    for stack in inv() or []:
        get = (lambda k: stack.get(k)) if isinstance(stack, dict) else (lambda k: getattr(stack, k, None))
        item_id, count = _item_entry(stack)
        if item_id:
            out.append({"slot": get("slot"), "item": item_id, "count": int(count or 1),
                        "selected": bool(get("selected"))})
    return out


def op_select_slot(req):
    fn = _fn("player_inventory_select_slot")
    return fn(int(req["slot"])) if fn else None


def op_slot_to_hotbar(req):
    fn = _fn("player_inventory_slot_to_hotbar")
    return fn(int(req["slot"])) if fn else None


def op_hand(_req):
    fn = _fn("player_hand_items")
    if not fn:
        return None
    hands = fn()
    main = getattr(hands, "main_hand", None) if not isinstance(hands, (list, tuple)) else (hands[0] if hands else None)
    if main is None:
        return None
    item_id, count = _item_entry(main)
    return {"item": item_id, "count": int(count or 0)}


def op_entities(req):
    fn = _fn("entities")
    if not fn:
        return None
    out = []
    for e in fn() or []:
        get = (lambda k: e.get(k)) if isinstance(e, dict) else (lambda k: getattr(e, k, None))
        pos = get("position")
        out.append({"name": str(get("name") or ""), "type": str(get("type") or ""),
                    "position": list(pos) if pos else None, "health": get("health"),
                    "uuid": str(get("uuid") or "")})
    return out[:100]


def op_targeted_block(req):
    fn = _fn("player_get_targeted_block")
    if not fn:
        return None
    tb = fn(req.get("max_distance", 20))
    if tb is None:
        return None
    get = (lambda k: tb.get(k)) if isinstance(tb, dict) else (lambda k: getattr(tb, k, None))
    pos = get("position")
    return {"position": list(pos) if pos else None, "block": str(get("type") or get("block") or "")}


def op_container_click(req):
    """Click one slot of the currently open container GUI, in-process.

    This is the container *write* primitive. minescript ships no
    container_click_slot (absent from minescript-forge-1.20.1-4.0.jar), which
    is why the old op forwarded to a function that does not exist and quietly
    returned null — and why `--body player` agent_take/agent_store could never
    move an item.

    The click must go through vanilla's own path:

        AbstractContainerScreen.slotClicked
          -> MultiPlayerGameMode.handleInventoryMouseClick(
                 containerId, slot, button, ClickType, player)   m_171799_

    because that method both calls AbstractContainerMenu.clicked (m_150399_)
    AND sends ServerboundContainerClickPacket. Calling m_150399_ directly —
    what this op did first — mutates the client menu only: the GUI shows the
    transfer while open, then the next container resync snaps every item back.
    Observed live: deposit two stacks, close, and `data get block 10 66 7`
    reports `Items: []` with all of it restored to the player.

    SRG names verified by javap against forge-1.20.1-47.4.16-client.jar plus
    the mcp_config tsrg2 (obf -> srg) mappings, cross-checked against the
    bytecode of evp.a(ccx,I,I,cbo) [AbstractContainerScreen.slotClicked]:
        Minecraft.getInstance       m_91087_
        Minecraft.gameMode          f_91072_   (field enn.r:Lffa;)
        Minecraft.player            f_91074_
        Player.containerMenu        f_36096_
        AbstractContainerMenu.containerId  f_38840_  (field cbf.j:I)
        MultiPlayerGameMode.handleInventoryMouseClick  m_171799_
        ClickType.PICKUP            PICKUP     (enum constants keep names)

    Field reads come back as object-table handles (java_access_field returns
    job.objects.retain(value)), so an int field is unboxed with java_to_string
    before it can be sent as a container id.

    Args: slot (menu index), button (default 0), click_type (default PICKUP).
      click_type PICKUP: pick up / put down the carried stack.
      QUICK_MOVE: shift-click (whole stack, routing both directions).
      SWAP: exchange with hotbar `button` (0-8) — the clean way to stage a
            single stack into hotbar then into a container slot.

    In-process rather than simulated mouse: OS-level clicks would have to
    focus the bot's window, and focus is exactly what the human must not lose.
    """
    cls_fn, member_fn, call_fn = _fn("java_class"), _fn("java_member"), _fn("java_call_method")
    field_fn, int_fn, str_fn = _fn("java_access_field"), _fn("java_int"), _fn("java_to_string")
    if not all((cls_fn, member_fn, call_fn, field_fn, int_fn, str_fn)):
        return {"ok": False, "reason": "java interop unavailable"}

    click_type = str(req.get("click_type") or "PICKUP").upper()
    if click_type not in CLICK_TYPES:
        return {"ok": False, "reason": f"unknown click_type {click_type!r}"}
    # A containerMenu handle is never null (the player always has the
    # InventoryMenu), so without this guard "click the chest" would silently
    # rearrange the player's own inventory instead of failing.
    if not _fn("screen_name")():
        return {"ok": False, "reason": "no container open"}

    try:
        slot, button = int(req["slot"]), int(req.get("button", 0))
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "reason": f"bad args: {exc}"}

    mc_cls = cls_fn("net.minecraft.client.Minecraft")
    menu_cls = cls_fn("net.minecraft.world.inventory.AbstractContainerMenu")
    label = "Minecraft.getInstance"
    try:
        mc = call_fn(mc_cls, member_fn(mc_cls, "m_91087_"))
        label = "Minecraft.player"
        player = field_fn(mc, member_fn(mc_cls, "f_91074_"))
        label = "Minecraft.gameMode"
        game_mode = field_fn(mc, member_fn(mc_cls, "f_91072_"))
        label = "Player.containerMenu"
        menu = field_fn(player, member_fn(cls_fn("net.minecraft.world.entity.player.Player"),
                                          "f_36096_"))
        # handleInventoryMouseClick drops the click on the floor unless the
        # containerId matches the menu the player actually has open. Every
        # java_* result is job.objects.retain(value) — an object-table id —
        # so the int read back here is a handle, not the id: feeding it
        # straight in made vanilla log "Ignoring click in mismatching
        # container. Click in 13, player has 1" and move nothing.
        label = "AbstractContainerMenu.containerId"
        container_id = int(str_fn(field_fn(menu, member_fn(menu_cls, "f_38840_"))))
        label = f"ClickType.{click_type}"
        ct_cls = cls_fn("net.minecraft.world.inventory.ClickType")
        click = field_fn(ct_cls, member_fn(ct_cls, click_type))
        label = f"handleInventoryMouseClick(slot={slot}, button={button}, {click_type})"
        gm_cls = cls_fn("net.minecraft.client.multiplayer.MultiPlayerGameMode")
        call_fn(game_mode, member_fn(gm_cls, "m_171799_"),
                int_fn(container_id), int_fn(slot), int_fn(button), click, player)
    except Exception as exc:
        return {"ok": False, "reason": f"{label}: {type(exc).__name__}: {exc}"}
    return {"ok": True, "slot": slot, "button": button, "click_type": click_type}


def op_screenshot(_req):
    fn = _fn("screenshot")
    if not fn:
        return None
    fn()
    return True


def op_close_screen(_req):
    """Close whatever GUI screen is open (Game Menu etc.) via Minecraft.setScreen(null).

    In-process Java interop — no OS key/click, so it never steals focus from
    the human's game window. show_chat_screen can't do this (it only closes
    ChatScreen), and Escape via xdotool lands on whichever window has focus.

    Two traps this avoids:

    * Screen.onClose() (m_7379_) is the wrong action — PauseScreen does not
      override it, so it runs Screen.onClose() -> Minecraft.popGuiLayer() and
      the menu stays up. Vanilla's "Back to Game" button calls
      Minecraft.setScreen(null) (SRG m_91152_), so that is the op.

    * Minecraft.f_91080_ (the screen field) must NOT be read: minescript's
      java_access_field hands back a truthy null-ish handle for object
      fields. Feeding that into Method.invoke produced the live
      NullPointerException, and trusting it made the op report "still open"
      while the screen was gone. screen_name() is the observable op_state
      already reports as open_screen, so the two can't disagree.
    """
    cls_fn, member_fn = _fn("java_class"), _fn("java_member")
    call_fn = _fn("java_call_method")
    screen_name = _fn("screen_name")
    if not all((cls_fn, member_fn, call_fn, screen_name)):
        return None
    # minescript's java_null is handle 0, so `is None` never matches a null
    # handle: a null target reaches Method.invoke and dies as
    # NullPointerException "Cannot invoke Object.getClass() because obj is null".
    null = _fn("java_null")
    if null is None:
        null = 0
    try:
        open_screen = screen_name()
    except Exception as exc:
        return {"closed": False, "reason": f"screen_name: {type(exc).__name__}: {exc}"}
    if not open_screen:
        return {"closed": False, "reason": "no screen"}
    label = "class Minecraft"
    try:
        cls = cls_fn("net.minecraft.client.Minecraft")
        label = "member m_91087_"
        get_instance = member_fn(cls, "m_91087_")
        label = "getInstance"
        mc = call_fn(cls, get_instance)
        label = "member m_91152_"
        set_screen = member_fn(cls, "m_91152_")
        label = "setScreen(null)"
        call_fn(mc, set_screen, null)
        label = "screen_name re-check"
        still_open = screen_name()
    except Exception as exc:
        return {"closed": False, "reason": f"{label}: {type(exc).__name__}: {exc}"}
    if still_open:
        return {"closed": False, "reason": f"still open after setScreen(null): {still_open}"}
    return {"closed": True}


def op_interop_probe(_req):
    """Walk the container-click interop chain and report where it breaks.

    Container *writing* needs
    `AbstractContainerMenu.m_150399_(slot, button, ClickType, player)`, which
    in turn needs live handles for `Minecraft.player` and
    `Player.containerMenu`. Both are OBJECT fields, and op_close_screen's
    docstring records that minescript's java_access_field hands back a truthy
    null-ish handle for those. Nothing else in this bridge reads an object
    field, so the whole container feature rests on one unverified assumption.

    This op is the experiment, so it must report per-step (the break point is
    the answer), must never raise out of a bad handle (that is the failure we
    are hunting), and must never mutate: no clicked(), no setScreen().

    SRG names come from javap against forge-1.20.1-47.4.16-client.jar and the
    mcp_config tsrg2 mappings, not from memory:
        Minecraft.getInstance          m_91087_
        Minecraft.player               f_91074_
        Player.containerMenu           f_36096_
        AbstractContainerMenu.getType  m_6772_
        AbstractContainerMenu.clicked  m_150399_   (probed for later, not called)
        ClickType.PICKUP               PICKUP      (enum constants keep names)
    """
    names = ("java_class", "java_member", "java_call_method",
             "java_access_field", "java_to_string")
    have = {n: bool(_fn(n)) for n in names}
    if not all(have.values()):
        return {"ok": False, "missing": [n for n, v in have.items() if not v],
                "steps": []}

    cls_fn, member_fn = _fn("java_class"), _fn("java_member")
    call_fn, field_fn = _fn("java_call_method"), _fn("java_access_field")
    to_str = _fn("java_to_string")

    steps = []
    memo = {}

    def klass(name):
        if name not in memo:
            memo[name] = cls_fn(name)
        return memo[name]

    def describe(handle):
        """Return (live_handle, detail); a null-in-Java handle is not live."""
        if handle is None:
            return None, "returned null"
        try:
            text = str(to_str(handle))
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if not text or text == "null":
            return None, f"toString -> {text!r}"
        return handle, text

    def run(name, fn):
        try:
            handle = fn()
        except Exception as exc:
            steps.append({"name": name, "ok": False,
                          "detail": f"{type(exc).__name__}: {exc}"})
            return None
        handle, detail = describe(handle)
        steps.append({"name": name, "ok": handle is not None, "detail": detail})
        return handle

    MC = "net.minecraft.client.Minecraft"
    PLAYER = "net.minecraft.world.entity.player.Player"
    MENU = "net.minecraft.world.inventory.AbstractContainerMenu"

    mc = run("Minecraft.getInstance",
             lambda: call_fn(klass(MC), member_fn(klass(MC), "m_91087_")))
    if mc is None:
        return {"ok": False, "steps": steps}

    player = run("Minecraft.player",
                 lambda: field_fn(mc, member_fn(klass(MC), "f_91074_")))
    if player is None:
        return {"ok": False, "steps": steps}

    menu = run("Player.containerMenu",
               lambda: field_fn(player, member_fn(klass(PLAYER), "f_36096_")))
    if menu is None:
        return {"ok": False, "steps": steps}

    run("AbstractContainerMenu.getType",
        lambda: call_fn(menu, member_fn(klass(MENU), "m_6772_")))
    run("ClickType.PICKUP",
        lambda: field_fn(klass("net.minecraft.world.inventory.ClickType"),
                         member_fn(klass("net.minecraft.world.inventory.ClickType"),
                                   "PICKUP")))

    return {"ok": all(s["ok"] for s in steps) and len(steps) == 5,
            "steps": steps}


OPS = {"probe": op_probe, "state": op_state, "chat": op_chat, "echo": op_echo,
       "chat_log": op_chat_log, "getblock": op_getblock, "container": op_container,
       "look_at": op_look_at, "press": op_press, "inventory_slots": op_inventory_slots,
       "select_slot": op_select_slot, "slot_to_hotbar": op_slot_to_hotbar,
       "hand": op_hand, "entities": op_entities, "targeted_block": op_targeted_block,
       "container_click": op_container_click, "screenshot": op_screenshot,
       "close_screen": op_close_screen, "interop_probe": op_interop_probe}


def chat_listener():
    """Capture incoming chat (Baritone status, quest toasts that echo to chat)."""
    eq_cls = _fn("EventQueue")
    if not eq_cls:
        return
    try:
        with eq_cls() as q:
            q.register_chat_listener()
            while True:
                event = q.get()
                msg = getattr(event, "message", None) or str(event)
                CHAT_SEQ["n"] += 1
                CHAT_LOG.append({"n": CHAT_SEQ["n"], "t": time.time(), "message": msg})
                # one-word kill switch: player types .quit in chat
                if ".quit" in msg.lower() and "[agent" not in msg and "[bridge" not in msg:
                    minescript.echo("[bridge] .quit received — shutting down")
                    os._exit(0)
    except Exception as exc:
        minescript.echo(f"[bridge] chat listener stopped: {exc}")


def main():
    threading.Thread(target=chat_listener, daemon=True).start()
    attempt = 0
    while True:  # reconnect forever: daemon restarts must not kill the bridge
        sock = None
        while sock is None:
            try:
                sock = socket.create_connection((HOST, PORT), timeout=10)
                attempt = 0
            except OSError as exc:
                attempt += 1
                if attempt == 1:
                    minescript.echo(f"[bridge] waiting for daemon at {HOST}:{PORT} ({exc}) ...")
                time.sleep(3)
        # connect timeout must NOT persist: the daemon can be silent for minutes
        # while the LLM plans — a lingering timeout kills recv() (live-run crash)
        sock.settimeout(None)
        minescript.echo("[bridge] connected — agent has control. \\killjob to stop.")
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    req = json.loads(line)
                    if req.get("op") == "stop":
                        sock.sendall(json.dumps({"id": req.get("id"), "ok": True, "result": "bye"}).encode() + b"\n")
                        return
                    try:
                        result = OPS[req["op"]](req)
                        resp = {"id": req.get("id"), "ok": True, "result": result}
                    except Exception as exc:
                        resp = {"id": req.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    sock.sendall(json.dumps(resp).encode() + b"\n")
        except OSError:
            # daemon died with unread data -> RST, not EOF. Same story as the
            # b"" above: connection is gone, fall through to the retry loop
            # instead of letting the exception kill this job.
            pass
        finally:
            sock.close()
            minescript.echo("[bridge] disconnected — retrying in 3s.")
        time.sleep(3)


if __name__ == "__main__":
    main()
