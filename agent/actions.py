"""Action executors — translate planner steps into bridge ops and verify results.

M2 implements: quest_checkmark, goto, goto_block, mine, collect (as mine-adjacent
pickup wait), wait_for, chat, manual. Crafting/smelting/GUI actions are M3+ and
report back as unsupported so the daemon escalates instead of pretending.

Every executor returns (ok: bool, note: str). Verification is state-based
(inventory deltas, position, quest progress), not trust-based.
"""
import json
import time

from executor import ActionExecutor

# verified against FTB docs + live run: change_progress is ONE word (underscore)
CHECKMARK_CMD = "/ftbquests change_progress @s complete {quest_id}"
CMD_ERRORS = ("permission", "unknown command", "no such", "incorrect argument",
              "usage:", "expected", "<--[here]")


def extract_player_lines(entries, player_name, bot_name="CoopBot"):
    """Which chat lines count as the player talking to the bot.

    Only lines FROM the player's username — the bot's own <Bot> lines, its
    "[agent]" broadcasts and server noise must never steer it. A player
    quoting our own prefix back at us is still noise, not an instruction.
    With no player_name configured, any non-agent line counts (singleplayer
    test bed).
    """
    out = []
    for e in entries or []:
        msg = e.get("message", "")
        if "[agent" in msg:
            continue
        if not msg.startswith("<"):
            continue
        sender, sep, text = msg[1:].partition(">")
        if not sep:
            continue
        if player_name and (sender != player_name or sender == bot_name):
            continue
        text = text.strip()
        if text:
            out.append((sender, text))
    return out


class Actions(ActionExecutor):
    def __init__(self, bridge, get_completed_quests, log, mp=False, mark_complete=None,
                 body="companion", player_name=None):
        self.bridge = bridge          # BridgeClient (daemon.py)
        self.get_completed = get_completed_quests
        self.log = log
        self.mp = mp                  # multiplayer: no save files, maybe no OP
        self.body = body              # "companion" (PlayerEngine entity) | "player" (own body)
        self.player_name = player_name  # whose chat lines count as instructions
        self.interrupted = []  # player lines that aborted the current wait
        self.chat_seen = 0  # chat high-water mark; the daemon syncs this to
        # what it already processed, so waits only react to newer lines
        self.mark_complete = mark_complete or (lambda qid: None)
        self.quest_titles = {}        # id -> human title (daemon fills from the graph)

    def say(self, text):
        """User-visible message, broadcast through world chat.

        `echo` only shows on the bot's own screen — joined over LAN, the
        human host would never see requests or progress. Never pass
        /commands here (chat would execute them); use the `chat` op
        directly for those.
        """
        self.bridge.call("chat", text=text)

    def interrupt(self):
        """Halt the body now. Player body: Baritone #stop. Companion body:
        /agent stop (PlayerEngine executor halt)."""
        if self.body == "companion":
            self.bridge.call("chat", text="/agent stop")
        else:
            self.bridge.call("chat", text="#stop")
        return True

    def _qname(self, qid):
        title = self.quest_titles.get(qid)
        return f"'{title}'" if title else qid

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _block_pos(at):
        """Block coords are integers — a fractional "player position" as `at`
        aims look_at into the next block over and the ray misses."""
        import math
        return {"x": math.floor(at["x"]), "y": math.floor(at["y"]),
                "z": math.floor(at["z"])}

    @staticmethod
    def _bare(item_id):
        # Minescript reports item ids without namespace ("cobblestone", not
        # "minecraft:cobblestone") — compare on the bare name everywhere
        return str(item_id).split(":")[-1]

    def _state(self):
        """state read that auto-dismisses a lingering GUI (Game Menu etc.).

        The bot's window can sit at the Game Menu; OS-level Escape/clicks
        would steal focus from the human's game, so the bridge closes it
        in-process (Screen.onClose) whenever we notice it during reads."""
        res = self.bridge.call("state") or {}
        if res.get("open_screen"):
            self.bridge.call("close_screen")
        return res

    def _inventory(self):
        raw = self._state().get("inventory", {})
        out = {}
        for k, v in raw.items():
            out[self._bare(k)] = out.get(self._bare(k), 0) + v
        return out

    def _position(self):
        return self._state().get("position")

    def _wait(self, predicate, timeout, poll=2.0, desc="", interrupt=True):
        deadline = time.time() + timeout
        self.interrupted = []
        first = self.bridge.call("chat_log", since=0)
        latest = first["latest"] if first else 0
        listen = bool(self.player_name) and interrupt
        if listen:
            # lines that arrived while planning (after the daemon's last
            # poll) are new instructions too — catch them before the loop
            fresh = [e for e in (first or {}).get("entries", [])
                     if e.get("n", 0) > self.chat_seen]
            lines = self._player_lines(fresh)
            if lines:
                self.interrupted = lines
                self.log(f"interrupted during {desc}: {lines[0][1][:100]}")
                return False
            self.chat_seen = latest
        since = latest
        while time.time() < deadline:
            if predicate():
                return True
            if listen:
                logs = self.bridge.call("chat_log", since=since) or {}
                since = logs.get("latest", since)
                lines = self._player_lines(logs.get("entries", []))
                if lines:
                    self.interrupted = lines
                    self.log(f"interrupted during {desc}: {lines[0][1][:100]}")
                    return False
            time.sleep(poll)
        self.log(f"timeout waiting for {desc} ({timeout}s)")
        return False

    def _wait_for_player_signal(self, timeout=900):
        """Player confirms a manual step by typing '.done' in Minecraft chat."""
        first = self.bridge.call("chat_log", since=0)
        since = first["latest"] if first else 0

        def check():
            nonlocal since
            logs = self.bridge.call("chat_log", since=since)
            if not logs:
                return False
            since = logs["latest"]
            self.chat_seen = max(self.chat_seen, since)
            # ignore our own "[agent]" prompts — they mention '.done' and the chat
            # listener captures echoes, so without this the wait confirms itself
            return any(".done" in e["message"].lower() and "[agent]" not in e["message"]
                       for e in logs["entries"])
        # no interrupt here: chatter ("almost done") must not kill a .done
        # wait — the confirmation IS the reply this flow is waiting for
        return self._wait(check, timeout, poll=2, desc="player to type .done in chat",
                          interrupt=False)

    def _wait_player_reply(self, timeout=180, poll=2):
        """Wait for ANY chat line from the player; return its text.

        Unlike _wait_for_player_signal (which needs the literal ".done"),
        this is the conversational wait: the player answering a question in
        their own words. Our own "[agent]" lines never count. Starts from
        the shared high-water mark, so a reply typed while planning still
        counts (it is not an interruption — it is the answer).
        """
        since = self.chat_seen
        deadline = time.time() + timeout
        while time.time() < deadline:
            logs = self.bridge.call("chat_log", since=since) or {}
            since = logs.get("latest", since)
            self.chat_seen = max(self.chat_seen, since)
            for _, text in self._player_lines(logs.get("entries", [])):
                return text
            time.sleep(poll)
        self.log(f"timeout waiting for player to reply in chat ({timeout}s)")
        return None

    def _player_lines(self, entries):
        """This actuator's view of "the player talked to us"."""
        return extract_player_lines(entries, self.player_name or "")

    def ask(self, step):
        """Ask the player a question in chat; the reply completes the step.

        Silence gets one repeat, then the step fails (never blocks the
        loop forever) — the next plan sees the unanswered question in the
        conversation history and can try differently.
        """
        question = step.get("question", "")
        self.say(f"[agent] {question}")
        self.log(f"ASK: {question}")
        reply = self._wait_player_reply(timeout=step.get("timeout", 180))
        if reply is None:
            self.say(f"[agent] still need an answer: {question}")
            reply = self._wait_player_reply(timeout=step.get("timeout", 180))
        if reply is None:
            return False, f"player never answered: {question}"
        return True, f"player answered: {reply}"

    def _baritone_idle(self, timeout):
        """Wait for a `#goto` to finish.

        Baritone does not reliably announce success: on a short path it prints
        only 'Going to: GoalBlock{...}' and then stops silently. Watching chat
        for Done/Finished alone therefore hangs until the timeout on a path
        that already completed (observed live: the bot arrived, no message
        followed, 180s burned). Baritone's real arrival signal is that the
        player's position stops changing, so accept that once pathing has
        demonstrably started.
        """
        marker = {"since": 0}
        first = self.bridge.call("chat_log", since=0)
        marker["since"] = first["latest"] if first else 0
        last, stable, started, moved = None, 0, False, False

        def check():
            nonlocal last, stable, started, moved
            logs = self.bridge.call("chat_log", since=marker["since"])
            if logs:
                marker["since"] = logs["latest"]
            for entry in (logs or {}).get("entries", []):
                msg = entry["message"]
                if "[Baritone]" not in msg:
                    continue
                if any(w in msg for w in
                       ("Done", "Finished", "canceled", "Cancelled", "No path", "Unable")):
                    self.log(f"baritone: {msg.strip()[:120]}")
                    return True
                if any(w in msg for w in ("Goal", "Going")):
                    started = True
            pos = self._position() or {}
            key = tuple(round(pos.get(k, 0), 2) for k in ("x", "y", "z"))
            if last is not None:
                if key == last:
                    stable += 1
                else:
                    moved, stable = True, 0
            last = key
            return stable >= 3 and (moved or started)
        return self._wait(check, timeout, desc="baritone completion")

    # -- executors ----------------------------------------------------------

    def quest_checkmark(self, step):
        """/save-all is unavailable in singleplayer and save files only flush on
        autosave, so verification is chat-response based in BOTH modes; the
        session/ledger record keeps the frontier moving until files catch up."""
        qid = step["quest_id"]
        before = self.bridge.call("chat_log", since=0)
        since = before["latest"] if before else 0
        self.bridge.call("chat", text=CHECKMARK_CMD.format(quest_id=qid))
        time.sleep(2)
        logs = self.bridge.call("chat_log", since=since) or {"entries": []}
        chat = " | ".join(e["message"] for e in logs["entries"])
        if any(w in chat.lower() for w in CMD_ERRORS):
            if self.mp:
                self.say(f"[agent] no OP for /ftbquests — click quest "
                         f"{self._qname(qid)} in the book, then type .done")
                if not self._wait_for_player_signal():
                    return False, f"no OP and player never confirmed quest {self._qname(qid)}"
                self.mark_complete(qid)
                return True, "completed manually (no OP on server)"
            return False, f"/ftbquests rejected: {chat[:150]}"
        self.mark_complete(qid)
        self.bridge.call("screenshot")  # capture the moment for the session gallery
        return True, "command accepted (recorded; save file confirms on next autosave)"

    def goto(self, step):
        x, y, z = step["x"], step["y"], step["z"]
        self.bridge.call("chat", text=f"#goto {int(x)} {int(y)} {int(z)}")
        ok = self._baritone_idle(timeout=step.get("timeout", 300))
        if self.interrupted:
            self.interrupt()
            return False, f"interrupted: {self.interrupted[0][1][:120]}"
        pos = self._position()
        if pos and abs(pos["x"] - x) <= 4 and abs(pos["z"] - z) <= 4:
            return True, f"arrived at {pos}"
        return ok, f"ended at {pos}"

    def goto_block(self, step):
        block = step["block"]  # full namespaced id — Baritone needs it for modded blocks
        self.bridge.call("chat", text=f"#goto {block}")
        ok = self._baritone_idle(timeout=step.get("timeout", 420))
        if self.interrupted:
            self.interrupt()
            return False, f"interrupted: {self.interrupted[0][1][:120]}"
        return ok, f"pathed toward nearest {block}"

    def mine(self, step):
        block = step["block"]
        count = int(step.get("count", 8))
        # count the item form of the block; modded drops may differ — daemon replans if so
        want_item = self._bare(step.get("collect_item", step["block"]))
        before = self._inventory().get(want_item, 0)
        self.bridge.call("chat", text=f"#mine {count} {block}")
        target = before + count

        def enough():
            return self._inventory().get(want_item, 0) >= target
        ok = self._wait(enough, timeout=step.get("timeout", 600), poll=5, desc=f"mine {block}")
        self.bridge.call("chat", text="#stop")
        have = self._inventory().get(want_item, 0)
        if self.interrupted:
            return False, f"interrupted: {self.interrupted[0][1][:120]}"
        return have > before, f"{want_item}: {before} -> {have} (wanted {target})"

    def collect(self, step):
        """Wait for an item to appear in the player's inventory (rewards, drops).
        Models also plan it for raw resources — so in companion mode, after a
        short grace period, fall back to mine-and-deliver via the companion."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))

        def enough():
            return self._inventory().get(item, 0) >= count
        if enough():
            return True, "already in inventory"
        self.say(f"[agent] waiting for {count}x {item} in YOUR inventory — "
                 f"if it's a quest reward, claim it in the book")
        grace = 45 if self.body == "companion" else step.get("timeout", 180)
        if self._wait(enough, timeout=grace, poll=3, desc=f"collect {item}"):
            return True, f"{item} x{self._inventory().get(item, 0)}"
        if self.interrupted:
            return False, f"interrupted: {self.interrupted[0][1][:120]}"
        if self.body != "companion":
            return False, f"{item} x{self._inventory().get(item, 0)}"
        self.log(f"  collect fallback: sending the companion to mine {item}")
        self.say(f"[agent] not appearing — Bob will go get {item} instead")
        ok, note = self.agent_mine({"block": step["item"], "count": count,
                                    "timeout": step.get("timeout", 600)})
        if not ok:
            return False, f"collect fallback failed: {note}"
        return self.agent_give({"item": item, "count": count})

    # -- M3: hands ------------------------------------------------------------

    def _tap(self, control, seconds=0.25):
        self.bridge.call("press", control=control, pressed=True)
        time.sleep(seconds)
        self.bridge.call("press", control=control, pressed=False)

    def _select_item(self, item_id):
        """Put the item in hand: pick from hotbar, or move it to the hotbar."""
        want = self._bare(item_id)
        slots = self.bridge.call("inventory_slots") or []
        match = next((s for s in slots if self._bare(s["item"]) == want), None)
        if not match:
            return False
        if match["slot"] <= 8:
            self.bridge.call("select_slot", slot=match["slot"])
        else:
            self.bridge.call("slot_to_hotbar", slot=match["slot"])
        time.sleep(0.4)
        hand = self.bridge.call("hand")
        return bool(hand and self._bare(hand.get("item", "")) == want)

    def place(self, step):
        item = step["item"]
        where = step.get("at") or step.get("where")
        if not isinstance(where, dict):  # descriptive location -> human decides spot
            return self.manual({"reason": f"place {item} at '{where}' (no coordinates given)"})
        if not self._select_item(item):
            return False, f"{item} not in inventory"
        x, y, z = where["x"], where["y"], where["z"]
        # aim at the top face of the block below the target cell
        self.bridge.call("look_at", x=x + 0.5, y=y - 0.05, z=z + 0.5)
        time.sleep(0.3)
        self._tap("use")
        time.sleep(0.5)
        placed = self.bridge.call("getblock", x=int(x), y=int(y), z=int(z)) or ""
        ok = self._bare(item).replace("_block", "") in self._bare(placed.split("[")[0])
        return (True, f"placed at {x},{y},{z}") if ok else \
               (False, f"block at target is {placed!r} — placement failed")

    def use_item_on(self, step):
        if not self._select_item(step["item"]):
            return False, f"{step['item']} not in inventory"
        target = step.get("at")
        if isinstance(target, dict):
            target = self._block_pos(target)
            self.bridge.call("look_at", x=target["x"] + 0.5, y=target["y"] + 0.5, z=target["z"] + 0.5)
            time.sleep(0.3)
        else:
            tb = self.bridge.call("targeted_block") or {}
            if self._bare(step.get("target", "")) not in self._bare(tb.get("block", "")):
                return self.manual({"reason": f"use {step['item']} on {step.get('target')} — "
                                              f"can't locate the target block, look at it for me"})
        self._tap("use")
        return True, "used"

    def inspect(self, step):
        """Report what block is at `at` (or what the bot looks at).

        Eyes for the planner: verify a machine/container is really there
        before clicking it, instead of failing blind and replanning blind.
        """
        target = step.get("at")
        if isinstance(target, dict):
            target = self._block_pos(target)
            block = self.bridge.call("getblock", x=target["x"], y=target["y"],
                                     z=target["z"])
            if isinstance(block, dict):
                block = block.get("block", block)
            return True, (f"block at {target['x']},{target['y']},{target['z']}: "
                          f"{block!r}")
        tb = self.bridge.call("targeted_block") or {}
        return True, (f"looking at: {tb.get('block', tb)!r} "
                      f"at {tb.get('position', '?')}")

    def interact(self, step):
        target = step.get("at")
        if isinstance(target, dict):
            self._safe_hand()
            target = self._block_pos(target)
            self.bridge.call("look_at", x=target["x"] + 0.5, y=target["y"] + 0.5, z=target["z"] + 0.5)
            time.sleep(0.3)
        else:
            tb = self.bridge.call("targeted_block") or {}
            if self._bare(step.get("block", "")) not in self._bare(tb.get("block", "")):
                return self.manual({"reason": f"interact with {step.get('block')} — "
                                              f"walk me to it or do it yourself"})
        before = (self.bridge.call("state") or {}).get("open_screen")
        self._tap("use")
        time.sleep(0.8)
        after = (self.bridge.call("state") or {}).get("open_screen")
        if step.get("gui"):
            return (after is not None and after != before), f"screen: {after!r}"
        return True, "interacted"

    def kill(self, step):
        want = self._bare(step["entity"])
        need = int(step.get("count", 1))
        killed = 0
        deadline = time.time() + step.get("timeout", 420)
        while killed < need and time.time() < deadline:
            ents = self.bridge.call("entities") or []
            pos = self._position() or {}
            def dist(e):
                p = e.get("position") or [1e9, 1e9, 1e9]
                return ((p[0] - pos.get("x", 0)) ** 2 + (p[2] - pos.get("z", 0)) ** 2) ** 0.5
            targets = [e for e in ents
                       if want in self._bare(e.get("type", "")) or want in e.get("name", "").lower()]
            if not targets:
                self.log(f"no {want} nearby — waiting")
                time.sleep(8)
                continue
            target = min(targets, key=dist)
            tp = target["position"]
            if dist(target) > 3.5:
                self.bridge.call("chat", text=f"#goto {int(tp[0])} {int(tp[1])} {int(tp[2])}")
                time.sleep(4)
                self.bridge.call("chat", text="#stop")
                continue
            self.bridge.call("look_at", x=tp[0], y=tp[1] + 1.2, z=tp[2])
            time.sleep(0.15)
            self._tap("attack", 0.1)
            time.sleep(0.6)
            still = [e for e in (self.bridge.call("entities") or []) if e.get("uuid") == target.get("uuid")]
            if not still or (still[0].get("health") or 1) <= 0:
                killed += 1
                self.log(f"killed {want} ({killed}/{need})")
        return killed >= need, f"killed {killed}/{need} {want}"

    def craft(self, step):
        """Verified collaboration until GUI crafting lands: ask the human, then
        confirm the item actually exists in inventory before moving on."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        if self._inventory().get(item, 0) >= count:
            return True, "already in inventory"
        for attempt in (1, 2):
            self.say(f"[agent] please CRAFT {count}x {item}, then type .done")
            self.log(f"CRAFT request: {count}x {item} (attempt {attempt})")
            if not self._wait_for_player_signal():
                return False, f"no confirmation for crafting {item}"
            have = self._inventory().get(item, 0)
            if have >= count:
                return True, f"crafted (verified {have} in inventory)"
            self.say(f"[agent] I only see {have}/{count} {item} — really crafted?")
        return False, f"{item} still missing after 2 confirmations"

    def smelt(self, step):
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        out = self._bare(step.get("output", step["item"]))
        self.say(f"[agent] please SMELT {count}x {item}, then type .done")
        if not self._wait_for_player_signal():
            return False, "no confirmation for smelting"
        return True, f"smelt confirmed ({self._inventory().get(out, 0)} {out} in inventory)"

    # -- PlayerEngine agent companion (agentlink mod) -------------------------

    def agent_state(self, timeout=10):
        """Query the companion via /agent state; parse the [agentstate] JSON line."""
        first = self.bridge.call("chat_log", since=0)
        since = first["latest"] if first else 0
        self.bridge.call("chat", text="/agent state")
        deadline = time.time() + timeout
        while time.time() < deadline:
            logs = self.bridge.call("chat_log", since=since) or {"entries": []}
            if logs.get("latest"):
                since = logs["latest"]
            for entry in logs.get("entries", []):
                msg = entry["message"]
                if "[agentstate]" in msg:
                    try:
                        return json.loads(msg.split("[agentstate]", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
            time.sleep(1.5)
        return None

    def _agent_inv(self, item):
        state = self.agent_state()
        if not state or not state.get("inventory"):
            return 0
        return state["inventory"].get(self._bare(item), 0)

    def agent_mine(self, step):
        """Companion mines ANY registered block (modded ids included). If the
        block is nowhere nearby, MineAndCollect just idles — so when the agent
        stands still without progress, we walk it along an expanding spiral
        and re-issue the mine task at each stop (real exploration)."""
        import math
        block, count = step["block"], int(step.get("count", 8))
        want = self._bare(step.get("collect_item", block))
        state0 = self.agent_state() or {}
        before = (state0.get("inventory") or {}).get(want, 0)
        target = before + count
        deadline = time.time() + step.get("timeout", 600)
        self.bridge.call("chat", text=f"/agent mine {block} {count}")
        home = state0.get("pos")
        last_pos, still_since = home, time.time()
        ring, leg = 90, 0
        while time.time() < deadline:
            time.sleep(12)
            st = self.agent_state() or {}
            have = (st.get("inventory") or {}).get(want, 0)
            if have >= target:
                return True, f"agent {want}: {before} -> {have}"
            pos = st.get("pos")
            if pos:
                home = home or pos
                if last_pos and max(abs(pos[0] - last_pos[0]), abs(pos[2] - last_pos[2])) > 3:
                    still_since = time.time()  # it's moving — let it work
                last_pos = pos
                if time.time() - still_since > 45:
                    ang = math.radians(leg * 47)
                    wx = int(home[0] + ring * math.cos(ang))
                    wz = int(home[2] + ring * math.sin(ang))
                    self.log(f"  agent idle — exploring toward {wx},{wz} (ring {ring})")
                    self.say(f"[agent] no {want} here — scouting {wx},{wz}")
                    self.bridge.call("chat", text=f"/agent run goto {wx} {int(pos[1])} {wz}")
                    leg += 1
                    if leg % 8 == 0:
                        ring += 70
                    time.sleep(40)  # travel window
                    self.bridge.call("chat", text=f"/agent mine {block} {count}")
                    still_since = time.time()
        self.bridge.call("chat", text="/agent stop")
        have = self._agent_inv(want)
        return have > before, f"agent {want}: {before} -> {have} (explored, wanted {target})"

    def agent_get(self, step):
        """Companion runs a full AltoClef gather/craft chain — VANILLA items only."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent run get {item} {count}")

        def enough():
            return self._agent_inv(item) >= count
        ok = self._wait(enough, timeout=step.get("timeout", 600), poll=15,
                        desc=f"agent getting {item}")
        if not ok:
            self.bridge.call("chat", text="/agent stop")
        return ok, f"agent has {self._agent_inv(item)}x {item}"

    def agent_give(self, step):
        """Direct agent->player transfer via /agent give (FTB quest tasks check
        the PLAYER inventory). Calls the agent over with follow first."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._inventory().get(item, 0)
        self.bridge.call("chat", text="/agent run follow")

        def close_and_given():
            self.bridge.call("chat", text=f"/agent give {item} {count}")
            time.sleep(2)
            return self._inventory().get(item, 0) >= before + count
        ok = self._wait(close_and_given, timeout=step.get("timeout", 120), poll=8,
                        desc=f"agent handing over {item}")
        self.bridge.call("chat", text="/agent stop")
        have = self._inventory().get(item, 0)
        return have > before, f"player {item}: {before} -> {have}"

    def agent_run(self, step):
        """Raw companion command: follow / goto x y z / attack <mob> / deposit / equip / stop."""
        self.bridge.call("chat", text=f"/agent run {step['cmd']}")
        return True, f"sent: {step['cmd']}"

    def agent_craft(self, step):
        """Server-side crafting from the AGENT's inventory — ANY recipe incl. modded."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent craft {step['item']} {count}")
        time.sleep(2)
        have = self._agent_inv(item)
        return have >= before + count or have >= count, \
            f"agent {item}: {before} -> {have}"

    def agent_take(self, step):
        """Pull items from containers/machine slots within 12 blocks of the agent."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent take {item} {count}")
        time.sleep(2)
        have = self._agent_inv(item)
        return have > before, f"agent {item}: {before} -> {have}"

    def agent_store(self, step):
        """Store items from the agent into the nearest chest (within 12 blocks)."""
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        before = self._agent_inv(item)
        self.bridge.call("chat", text=f"/agent store {item} {count}")
        time.sleep(2)
        have = self._agent_inv(item)
        return have < before or before == 0, f"agent {item}: {before} -> {have}"

    @staticmethod
    def _split_listing(listing, own):
        """Split a container GUI's merged slot list into (container, player).

        A container screen lists the container's own slots first, then the
        player's 36 inventory slots on top of them (measured live on a single
        chest: 0..26 chest, player main 9..35 -> 27..53, hotbar 0..8 ->
        54..62). So walking from the highest index down and consuming `own`
        accounts for exactly the player's slots and leaves the container's.

        Without this, taking sand while already holding some picks the
        player's own stack, and QUICK_MOVE pushes it INTO the chest while the
        action reports a successful pull.
        """
        left = {}
        for key, val in (own or {}).items():
            left[Actions._bare(key)] = left.get(Actions._bare(key), 0) + int(val)
        here, mine = [], []
        for entry in sorted(listing or [], key=lambda e: -(e.get("slot") or 0)):
            item, count = Actions._bare(entry.get("item")), int(entry.get("count") or 1)
            if left.get(item, 0) >= count:
                left[item] -= count
                mine.append(entry)
            else:
                here.append(entry)
        return here, mine

    @staticmethod
    def _reach(pos, at):
        """Rough eye-to-block distance; MC's use range is 4.5 blocks."""
        dx = pos.get("x", 0) - at["x"]
        dy = (pos.get("y", 0) + 1.6) - (at["y"] + 0.5)
        dz = pos.get("z", 0) - at["z"]
        return (dx * dx + dy * dy + dz * dz) ** 0.5

    def _approach(self, at):
        """Get within reach of `at` WITHOUT pathing onto it.

        Baritone's allowBreak defaults to on, so `#goto <the container block>`
        mines the container to stand on its own block — observed live: the test
        chest at (10,66,7) vanished mid-action along with its contents. So skip
        the walk when already close, and otherwise aim at an air block beside
        the container instead.
        """
        pos = self._position() or {}
        if max(abs(pos.get("x", 0) - at["x"]), abs(pos.get("z", 0) - at["z"])) <= 3:
            return True
        for dx, dz in ((0, 1), (1, 0), (0, -1), (-1, 0),
                       (1, 1), (1, -1), (-1, 1), (-1, -1)):
            x, z = at["x"] + dx, at["z"] + dz
            block = self.bridge.call("getblock", x=x, y=at["y"], z=z) or ""
            if self._bare(block.split("[")[0]) in ("air", "cave_air", "void_air"):
                self.bridge.call("chat", text=f"#goto {x} {at['y']} {z}")
                return self._baritone_idle(timeout=180)
        self.log(f"no standable air beside {at['x']},{at['y']},{at['z']}")
        return False

    def _safe_hand(self):
        """Select an empty hotbar slot before right-clicking a block.

        Right-clicking with a usable item held (guide book, crafting
        table...) fires the ITEM when the ray misses the block — observed
        live: aiming up at an elevated chest grazed its pillar and opened
        'Materials and You' instead of the chest. An empty hand misses
        safe. Best effort: False when every hotbar slot is full.
        """
        slots = self.bridge.call("inventory_slots") or []
        used = {s.get("slot") for s in slots if s.get("slot") in range(9)}
        if next((s.get("slot") for s in slots if s.get("selected")),
                None) not in used:
            return True  # already holding nothing usable
        free = [s for s in range(9) if s not in used]
        if not free:
            return False
        self.bridge.call("select_slot", slot=free[0])
        return True

    def _aim_note(self):
        """What the crosshair is on right now, for failure notes."""
        tb = self.bridge.call("targeted_block") or {}
        block = tb.get("block") if isinstance(tb, dict) else tb
        return self._bare(block) if block else "nothing"

    def _open_container(self, at):
        """Aim at the container block and right-click it. None on success,
        else a short reason string.

        Reads `state` raw rather than through _state(), which closes any open
        screen on sight — that would undo the click we just made.
        """
        self.bridge.call("close_screen")
        self._safe_hand()
        at = self._block_pos(at)
        self.bridge.call("look_at", x=at["x"] + 0.5, y=at["y"] + 0.5, z=at["z"] + 0.5)
        time.sleep(0.3)
        self._tap("use")
        time.sleep(0.8)
        screen = (self.bridge.call("state") or {}).get("open_screen")
        aim = self._aim_note()
        if not screen:
            return (f"nothing opened aiming at {aim} "
                    f"(ray missed the container or the block is gone)")
        if self.bridge.call("container") is None:
            return (f"screen {screen!r} exposes no container slots "
                    f"(aiming at {aim})")
        return None

    def _stand_ready(self, at):
        """Walk to a container block and confirm it is within use range.

        None when it can be opened, else why it cannot.
        """
        if not (isinstance(at, dict) and "x" in at):
            return "no coordinates for the container"
        if not self._approach(at):
            return f"could not get near the container at {at['x']},{at['y']},{at['z']}"
        reach = self._reach(self._position() or {}, at)
        if reach > 4.5:
            return f"still {reach:.1f}m from the container at {at['x']},{at['y']},{at['z']}"
        return None

    def take_from_chest(self, step):
        """Open a known container and QUICK_MOVE the requested items out.

        `at` is the container block itself (the survey records the block the
        player was looking at when they opened it, not their feet), so this
        aims and clicks rather than asking the human to do it for us.
        """
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        at = step.get("at") or {}
        not_ready = self._stand_ready(at)
        if not_ready:
            return False, not_ready

        before = self._inventory().get(item, 0)   # _state() dismisses any stray GUI
        blocked = self._open_container(at)
        if blocked:
            self.bridge.call("close_screen")
            return False, (f"cannot open the container at "
                           f"{at['x']},{at['y']},{at['z']}: {blocked}")

        wanted, pulled = count, 0
        while pulled < wanted:
            listing = self.bridge.call("container") or []
            own = (self.bridge.call("state") or {}).get("inventory", {})
            container, _ = self._split_listing(listing, own)
            candidates = [e for e in container
                          if self._bare(e["item"]) == item and e.get("slot") is not None]
            if not candidates:
                break
            # _split_listing hands back slots descending, so next() would grab
            # the small stack first and then need the big one too — asking for
            # 64 from a chest holding 16+64 pulled all 80. Take the biggest
            # stack that still fits the remainder; if none fits, the smallest
            # one, which is the least over-pull a whole-stack click can do.
            fits = [e for e in candidates if int(e.get("count") or 1) <= wanted - pulled]
            entry = (max(fits, key=lambda e: int(e.get("count") or 1)) if fits
                     else min(candidates, key=lambda e: int(e.get("count") or 1)))
            res = self.bridge.call("container_click", slot=entry["slot"], button=0,
                                   click_type="QUICK_MOVE") or {}
            if not res.get("ok"):
                self.bridge.call("close_screen")
                return False, f"container_click failed: {res.get('reason')}"
            pulled += int(entry.get("count") or 1)
            time.sleep(0.3)
        self.bridge.call("close_screen")
        time.sleep(0.5)

        have = self._inventory().get(item, 0)
        moved = have - before
        if moved <= 0:
            return False, f"{item} not in that container"
        if moved < count:
            return False, f"only pulled {moved}/{count} {item}: {before} -> {have}"
        return True, f"took {moved} {item}: {before} -> {have}"

    def put_in_chest(self, step):
        """Open a known container and QUICK_MOVE the player's own stacks into it.

        The deposit half of take_from_chest: same reach/approach/open/close
        discipline, but the click lands on the player's half of the slot list
        (27..62), where vanilla's QUICK_MOVE routes the stack into the
        container. A whole stack moves per click, so asking for 16 can push
        64 — the note reports what actually moved.
        """
        item, count = self._bare(step["item"]), int(step.get("count", 1))
        at = step.get("at") or {}
        not_ready = self._stand_ready(at)
        if not_ready:
            return False, not_ready

        before = self._inventory().get(item, 0)
        if before <= 0:
            return False, f"player has no {item} to deposit"
        blocked = self._open_container(at)
        if blocked:
            self.bridge.call("close_screen")
            return False, (f"cannot open the container at "
                           f"{at['x']},{at['y']},{at['z']}: {blocked}")

        wanted, pushed = count, 0
        while pushed < wanted:
            listing = self.bridge.call("container") or []
            own = (self.bridge.call("state") or {}).get("inventory", {})
            _, mine = self._split_listing(listing, own)
            candidates = [e for e in mine
                          if self._bare(e["item"]) == item and e.get("slot") is not None]
            if not candidates:
                break
            # same rule as take: the biggest stack that still fits the
            # remainder, else the smallest one (least over-pull a whole-stack
            # click can do).
            fits = [e for e in candidates if int(e.get("count") or 1) <= wanted - pushed]
            entry = (max(fits, key=lambda e: int(e.get("count") or 1)) if fits
                     else min(candidates, key=lambda e: int(e.get("count") or 1)))
            res = self.bridge.call("container_click", slot=entry["slot"], button=0,
                                   click_type="QUICK_MOVE") or {}
            if not res.get("ok"):
                self.bridge.call("close_screen")
                return False, f"container_click failed: {res.get('reason')}"
            pushed += int(entry.get("count") or 1)
            time.sleep(0.3)
        self.bridge.call("close_screen")
        time.sleep(0.5)

        have = self._inventory().get(item, 0)
        moved = before - have
        if moved <= 0:
            return False, f"nothing moved — is that container full of {item}?"
        if moved < count:
            return False, f"only pushed {moved}/{count} {item}: {before} -> {have}"
        return True, f"deposited {moved} {item}: {before} -> {have}"

    def wait_for(self, step):
        seconds = step.get("seconds", 30)
        self.log(f"wait_for: {step.get('condition', '')} — sleeping {seconds}s")
        time.sleep(seconds)
        return True, "waited"

    def chat(self, step):
        self.bridge.call("chat", text=step["text"])
        return True, "sent"

    def manual(self, step):
        reason = step.get("reason", "unsupported action")
        self.say(f"[agent] NEED HELP: {reason} — do it, then type .done in chat")
        self.log(f"MANUAL ESCALATION: {reason}")
        if not self._wait_for_player_signal():
            return False, "player never confirmed manual step"
        return True, "human handled it"

    # models drift across body modes despite prompt rules — route structurally.
    # companion mode: resource work goes to the PlayerEngine entity;
    # player mode: agent_* actions fold back onto the player's own body.
    COMPANION_REMAP = {"mine": "agent_mine", "goto_block": "agent_mine"}
    PLAYER_REMAP = {"agent_mine": "mine", "agent_craft": "craft",
                    "agent_get": "craft", "agent_take": "manual", "agent_store": "manual"}

    def _remap_for_body(self, name, action):
        if self.body == "companion":
            if name in self.COMPANION_REMAP and action.get("block"):
                action.setdefault("count", 2 if name == "goto_block" else 8)
                return self.COMPANION_REMAP[name]
            return name
        # player mode
        if name == "agent_give":
            return "_noop_already_player"  # items are already in the player's inventory
        if name == "agent_run":
            cmd = str(action.get("cmd", "")).split()
            if cmd and cmd[0] == "goto" and len(cmd) >= 4:
                action.update(x=float(cmd[1]), y=float(cmd[2]), z=float(cmd[3]))
                return "goto"
            if cmd and cmd[0] == "attack":
                action["entity"] = cmd[1] if len(cmd) > 1 else ""
                return "kill"
            if cmd and cmd[0] == "stop":
                self.bridge.call("chat", text="#stop")
            return "_noop_already_player"  # follow/pickup_drops/etc are meaningless solo
        if name in self.PLAYER_REMAP:
            if self.PLAYER_REMAP[name] == "manual":
                action["reason"] = f"{name} has no player-body equivalent: {action}"
            return self.PLAYER_REMAP[name]
        return name

    def _noop_already_player(self, step):
        return True, "no-op in player-body mode"

    def run(self, step):
        action = dict(step["action"])
        name = action.pop("action")
        new_name = self._remap_for_body(name, action)
        if new_name != name:
            self.log(f"  (remapped {name} -> {new_name} for body={self.body})")
            name = new_name
        handler = getattr(self, name, None)
        if not handler:
            return self.manual({"reason": f"unknown action '{name}': {action}"})
        health_before = (self.bridge.call("state") or {}).get("health")
        ok, note = handler(action)
        health_after = (self.bridge.call("state") or {}).get("health")
        if health_before and health_after and health_after < min(6, health_before - 8):
            self.say("[agent] taking heavy damage — pausing, type .done when safe")
            self.log(f"DANGER: health {health_before} -> {health_after}")
            self._wait_for_player_signal(timeout=300)
        return ok, note
