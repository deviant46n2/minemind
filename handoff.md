# Handoff — CoopBot (MineMind fork → AI co-op companion for EMC SubTerra)

_Last updated: 2026-09-26 12:40. Bot client RUNNING (hidden): java pid
2078902, game window 23068679 (unmapped), bridge job alive on 48750,
human window 14680071 focused. llama-server :8081 (Qwen3-14B) up, health 200._

## What this is

Separate Minecraft player `CoopBot` joins the user's modded world over LAN and
acts as an AI teammate: plans with a local LLM, runs real actions (mine via
Baritone, FTB quest checkmarks, craft as human-assist), reads real world state.
Target = EMC SubTerra Tech (MC 1.20.1 / Forge 47.4.16, FTB Quests).

## PROVEN working (do not re-investigate)

1. **Join**: `python3 tools/launch.py --join 127.0.0.1:25565 --heap 6G --wait --shot <name>`
   (mcwifipnp offline auth, LAN port 25565). Proof PNGs in `data/proof/`.
2. **FTB quest checkmark — ROOT CAUSE FIXED**: `/ftbquests change_progress`
   needs op level ≥2; bot was op 0 → brigadier `<--[HERE]`. User ran
   `/op CoopBot` (ops.json level 2). Syntax was always correct:
   `/ftbquests change_progress @s complete {quest_id}`.
   End-to-end proof: `--plan plan_checkmark.json` → OK + save read-back
   (`saves/nis/ftbquests/d1506c4b-*.snbt`, 142 completed incl. target).
3. **state/frontier**: `op_state` None-guard fixed (TDD). Needs
   `AGENT_MC_DIR` env or frontier silently = 0.
4. **Baritone installed** (bot instance only, user-approved):
   `instance/mods/baritone-standalone-forge-1.10.3.jar` (SHA1 verified).
   `--plan plan_mine.json` → `cobblestone 0->2` in 7s, exit 0
   (`/tmp/opencode/daemon_mine.log`).
5. **Full LLM run** (`/tmp/opencode/daemon_full4.log`, killed at step 3):
   cycle 1 planned 8 steps (Need Lava?/Spout/Altar) →
   craft porcelain_crucible **verified** → quest `37C34C4D65754C78` checkmark OK
   → step 3 craft `create:spout` waiting for user when session ended.
6. **Human-assist craft contract (works)**: user runs
   `/give CoopBot <item>` **then types `.done` in their own chat**.
   Verification = item in **bot's** inventory (yours doesn't count).
   `.done` detection proven (attempt boundaries 00:11:28 / 00:21:16 in log).
   Note: `craft`/`smelt` are 900s ×2 waits; LLM loves picking craft as step 1.
7. **Game Menu does NOT block daemon ops**: chat/execute/state are game-side
   injections (proven with menu open). It only breaks OS-level input and
   `player_press_*` keybinds. **Never send global Escape/xdotool key — it
   lands on the human's game and opens THEIR menu.**

## RESOLVED this session: Game Menu at join + two-window focus (user rejected 2 windows)

**Root cause of the menu (bytecode-proven, not autorun):** `Options.f_92126_`
= `pauseOnLostFocus` (read/written in `options.txt`). `GameRenderer.m_109093_`
(render): option true + window inactive >500ms → `Minecraft.m_91358_(false)`
→ news `PauseScreen`. The bot's window lost focus on every join → menu.
**Fix: bot instance `instance/options.txt` line 57 = `pauseOnLostFocus:false`.
Never change the user's instance — theirs must stay true.**

**Fixes, all live-proven:**
- `op_close_screen` rewritten (5 tests): `Minecraft.m_91152_(null)` (setScreen,
  handle 0 = minescript `java_null`), pre/post via `screen_name()`.
  **NEVER read field `f_91080_`** — `java_access_field` returns a *truthy*
  null-ish handle, which caused the old NPE and a false "still open" report.
  `Screen.onClose` (m_7379_) is wrong for PauseScreen (`popGuiLayer` instead).
  E2E proof: synthetic Escape to the *bot* window → menu opens with focus
  untouched → `close_screen` → `{closed: true}` → `state.open_screen=None` →
  jump works (64.0→65.3 ×4).
- **Bot window hidden, ops work in-process**: `xdotool windowunmap` hides it;
  state/jumps/chat all still work; only one game window visible.
  Negative coords are impossible (X screen 2560x1440) — unmap, not off-screen.
- `tools/launch.py`: `set_hidden(pid, hidden)` / `shot(name, pid)` / `--show`,
  auto-hide after `wait --shot`, grim proof by temporarily re-mapping.
  **Focus guard is mandatory**: after `windowmap` the WM steals focus (its own
  focus-follows-map beats a single `windowfocus`) → human Game Menu in 500ms.
  Guard thread pulls focus off the bot while it's mapped (probe: 947ms bot
  focus before → **30ms** after, threshold 500ms).
- **Bridge restart without user typing**: `\agent_bridge` via synthetic keys —
  `xdotool key --window <bot> t`, `xdotool type --window <bot> --delay 40
  '\agent_bridge'`, `key --window <bot> Return` (works unfocused, same
  mechanism as the Escape test). Killed java PID + relaunched `launch.py`
  also works (autorun `[*]=\agent_bridge` on join) and does NOT steal focus.
- **Bridge survived RST (bug fixed)**: `ConnectionResetError` in `sock.recv`
  escaped the reconnect loop → job exited code 1 after any probe script died
  abruptly. `except OSError: pass` around the recv loop (TDD:
  `test_survives_connection_reset`, SO_LINGER-0 RST against a real socket).

Proofs: `data/proof/relaunch_hidden.png` (launch auto-hide), `focus_probe.png`
(shot focus-guard), `final_single_window.png` (one visible game window,
human in-world, no menu), `single_window.png`.

## Next steps (ordered)

1. Relaunch daemon full run:
   ```bash
   cd "/home/deviant/Projects/coop bot"
   nohup env AGENT_MC_DIR="/home/deviant/.var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher/instances/EMC SubTerra Tech/minecraft" \
     LOCAL_LLM_URL=http://127.0.0.1:8081/v1/chat/completions LOCAL_LLM_MODEL=qwen3-14b \
     python3 agent/daemon.py --max-cycles 3 --body player \
     > /tmp/opencode/daemon_full5.log 2>&1 &
   ```
   Watch: connect-time close fires; craft steps need user `/give` + `.done`.
   **`--body player` is mandatory** (spec = companion as a real player;
   `--body companion` spawns a PlayerEngine NPC and agentlink isn't installed).
2. Make the bridge-job restart procedure mechanical: when killing the bot
   client, also `pgrep -f "coop bot/instance/minescript" | xargs -r kill -9`
   (kill -9 java leaves the minescript python child alive — see Gotchas).
3. **Git checkpoint** (project AGENTS requires it; user hasn't said commit
   yet — propose it): modified `.gitignore README.md agent/{actions,daemon,llm}.py
   bridge/minescript/agent_bridge.py tools/{build_quest_graph,launch}.py
   instance/options.txt`; untracked `test/ tools/setup_instance.py` + jar.
4. Known gaps worth a decision later: craft verification requires item
   transfer to the bot (no direct MP inventory give without `/give`);
   human-assist waits stall LLM cycles up to 30 min per craft.

## Gotchas (carried forward — each cost us time)

- **Only ONE bridge may exist**: a stray standalone `python3
  instance/minescript/agent_bridge.py` (debug leftover, pid 1530592) raced the
  in-game bridge for port 48750 and caused the `NoneType` state errors.
  **This session's variant: `kill -9 <java pid>` does NOT kill the minescript
  python child** (it runs as a separate process) — the orphan keeps retrying
  48750 and answers with a dead game → `TypeError: 'NoneType' object is not
  iterable` on `state`/`press`. Kill both: java pid + the python whose cmdline
  contains `coop bot/instance/minescript`. Check: `pgrep -af agent_bridge`.
- `state` returning `ok:false` is easy to mask — probes must check `ok`, not
  just `.get("result") or {}` (that turned a dead-bridge TypeError into a
  silent `position=None`).
- **Kill by exact PID only**, cmdline must contain `coop bot/instance`.
  Never `pkill -f` (self-match kills shell), never broad `pgrep -ax java`
  (killed the user's client once). `kill -9`, no SIGTERM, no sleep after.
- Bridge is single-connection: while the daemon holds 48750, external diag
  scripts cannot bind — stop the daemon first.
- zsh: `echo ===` breaks (= expansion); quote it. `DISPLAY=:1` for xdotool
  (XWayland); `grim` for full-screen shots (launch.py uses it);
  `import -window <xid>` for one window.
- `find_world`/`completed_quests` need `AGENT_MC_DIR` (else silently 0).
- Window IDs are reused across client restarts — re-query before input.
- Tests: `for t in test/test_*.py; do python3 $t; done` (all runnable as
  standalone scripts, no pytest dependency).
- User's game client = do not touch (window "SubTerra Tech: V8.9", user
  `Deviant46n2`, window 14680071, java pid from Prism — never kill it, never
  send it keys). Bot shares that title: bot = the window whose
  `_NET_WM_PID` is the `coop bot/instance` java pid. Keys only ever go to the
  bot window (`xdotool key --window <bot>` works without focus).

## Layout (essentials)

```
agent/daemon.py        daemon: BridgeClient, run_plan, --body, --plan, frontier loop
agent/actions.py       action impls; CHECKMARK_CMD (correct); _state() auto-close;
                       craft/smelt human-assist; _baritone_idle (chat-driven)
agent/world_state.py   find_world + completed_quests (reads ftbquests/**/*.snbt)
agent/llm.py           local LLM (OpenAI-compatible chat completions)
bridge/minescript/     op_state guard, op_close_screen (SRG interop), chat listener
  agent_bridge.py      → synced copy lives in instance/minescript/
instance/              bot's game dir: mods/{baritone,minescript}, saves, minescript/
tools/launch.py        spawn+join+wait+set_hidden/shot (focus guard, auto-hide,
                       --show, grim proof into data/proof/)
tools/setup_instance.py  instance staging (minescript jar, etc.)
test/                  6 suites, all green (launch, bridge_reconnect incl. RST,
                       bridge_state, close_screen, build_quest_graph, llm_local)
data/proof/            relaunch_hidden.png, focus_probe.png,
                       final_single_window.png (one visible game window)
README.md              rewritten for the CoopBot fork (verified checklist)
/tmp/opencode/         session logs: daemon_full4.log, daemon_mine.log,
                       launch_*.log, monitor.log, probe_{e2e,hidden}.py,
                       test_shot_focus.py, minescript_javap.txt, plan_*.json
```
