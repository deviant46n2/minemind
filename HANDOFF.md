# HANDOFF — CoopBot session 2026-09-28 (playerengine-spike)

## Where everything is

- Branch: `playerengine-spike` (12 commits today on top of `master@db09927`).
  Working tree clean. `master` untouched. Test suite: 13 files, all green.
- Running: **llama-server only** (pid 835804, Qwen3-14B-Q4_K_M, :8081, alias
  `qwen3-14b`). Bot client, bridge, daemon all shut down with the game.
- `data/base_sp.json` (gitignored, runtime state): 2 surveyed containers +
  4 knowledge notes. Do not delete — it's the bot's entire base knowledge.

## Resume order (strict — each layer needs the one below)

1. LLM already up (`curl 127.0.0.1:8081/health`). If not:
   `/home/deviant/qwen38-experimental/adaptive-kv/build/bin/llama-server -m
   /home/deviant/models/Qwen3-14B-Q4_K_M.gguf --alias qwen3-14b
   --ctx-size 16384 -ngl 99 -fa on --jinja -np 1 --host 127.0.0.1 --port 8081`
2. User opens world to LAN (mcwifipnp pins 25565, offline auth).
3. Bot client joins: `python3 tools/launch.py --join 127.0.0.1:25565
   --heap 6G --wait` (hidden window; `--show` to watch). Joins as `CoopBot`.
   Needs `/op CoopBot` once per world (quest checkmarks are op-gated; symptom
   is `<--[HERE]` at command end = not opped, not malformed).
4. Brain (one at a time — second launch dies on port 48750):
```
cd "/home/deviant/Projects/coop bot"
AGENT_MC_DIR="/home/deviant/.var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher/instances/EMC SubTerra Tech/minecraft" \
LOCAL_LLM_URL=http://127.0.0.1:8081/v1/chat/completions LOCAL_LLM_MODEL=qwen3-14b RECLAMATION_LLM=local \
python3 agent/daemon.py --mode chores --max-cycles 1000 --body player --player-name Deviant46n2
```
5. Talk to him in game chat. Everything the player says is an instruction.
   No `.goal` needed (still works). He asks when unsure, waits silently idle.

## Architecture (current)

```
chat -> run_chores (daemon.py) -> planner (Qwen3-14B local) -> plan JSON
  -> run_plan -> Actions.run (actions.py, implements ActionExecutor)
  -> bridge TCP 48750 -> agent_bridge.py (minescript child of game JVM)
  -> game (Baritone #chat cmds, container clicks, minescript state reads)
```
- `--mode chores` (default): conversation-driven, never quests unless asked.
  `--mode quests` restores the old FTB-frontier loop.
- Brain never touches movement: LLM emits step dicts; executor translates.
  Player-body needs nothing on the host. Companion body (`/agent ...`
  commands) exists in code but its backend is NOT deployed (see below).

## Proven live today (server-NBT-verified where noted)

- take/put sand vs test chest, incl. far-start Baritone walk (player 80→144,
  chest 16, then full clear; `/data` confirmed both directions).
- Deposit 768 sand into the user's crusher input chest (5,69,0), elevated
  and occluded — via aim-search open.
- Output chest (5,65,0): 150 kelp + 163 glowstone + 299 dust + 1 gravel out.
- Transmutation table (11,66,6): output ghosts materialize full stacks on
  QUICK_MOVE; player items QUICK_MOVEd in are learned/burned (7 cactus →
  cactus ghost appeared, grid re-sorted). take/put work on it. Search box
  untypable (no primitive). Ghost slots SHIFT on re-sort — scan by name.
- Bot holds (in HOST playerdata — safe while their world is intact):
  kelp/glowstone/dust + tools, book, table, crucible, cactus, coal, etc.

## Base knowledge (data/base_sp.json)

- Containers: input chest 5,69,0 (elevated, stand below ~4.7,66,0.3);
  output chest 5,65,0.
- Notes: table mechanics (above); lectern (Ars Nouveau) at 11,66,7 —
  custom screen, container hands probably don't apply, UNTESTED.

## Open problems (ranked)

1. **Bot drops from LAN (4x today).** Signature varies; once "Error receiving
   packet 5". Coincides with heavy click activity but unproven. Rejoin =
   relaunch client + restart daemon. If it recurs: capture bot
   `instance/logs/latest.log` lines around the drop BEFORE relaunching.
2. **Hand-state mystery (partially open).** A full hotbar + held book made
   every miss open 'Materials and You'. `_safe_hand` (select empty / stash
   to main via SWAP) + `empty_hand` action + per-attempt `hand check:` log
   line exist; west-face aim + full `_open_container` proven live twice.
   If book screens recur, the hand-check line shows exactly what he held.
3. **Transmutation EMC blindness.** Can't read pool value; dead clicks =
   empty pool → tell player to charge it. Klein-star/fuel slots untouched.
4. **Planner judgment (14B).** Needs the new prompt blooded (reply action,
   GUI boundary, worked examples went live late, ~2 live turns). Open
   question: 14B+examples vs 27B (`Qwen3.8-27B-Uncensored-Q3_K_M.gguf`,
   13.5GB on disk — VRAM-tight, needs 14B stopped first). A/B offline on
   identical prompts, keep winner. Frontier APIs need keys (none configured).
5. **Base blindness.** Only 2 chests + 4 notes known. "Sand" once meant a
   beach 200 blocks away. Keep drip-feeding coords+contents (F3 + open it).
6. **Lectern + crusher controller GUI** untested. Table search box impossible.

## PlayerEngine verdict (investigated, parked)

Source-verified (Player2NPC `forge` + PlayerEngine + MineMind, in
/tmp/opencode — delete if needed): embodiment real (AutomatoneEntity, NBT
persist, per-player map), command firewall real (get/goto/follow/attack/
farm/fish/deposit/equip/stash/stop…), cloud bypassable (Character is a
trivial record; agentlink shim already in repo, inherited from MineMind).
Blockers: `get`/craft chains are VANILLA-ONLY catalogue (~65 entries —
useless in this pack: no vanilla logs/ores); jars NOT in repo
(agentlink-mod/libs empty, needs 1.3.0 but upstream has 1.0.5); nothing
installed on host; never smoke-tested (mixin clash vs 204 mods? greeting
phone-home? 47.2.0-built code on 47.4.16?). Decision: **B-narrow** —
`agent/executor.py` seam committed (ActionExecutor ABC, Actions implements,
`interrupt()` unifies #stop//agent stop). No PlayerEngineExecutor class
until jars load in the pack (untestable code is fiction). Next gate if
wanted: fetch jars → build agentlink → install 3 jars on host → smoke
`/agent spawn/follow/stop`. Until then outcome A holds by fact.

## Gotchas (learned the hard way)

- Kill with `kill -9 <pid>` (SIGKILL). NEVER `pkill -f` (matches own shell).
  Never touch the human's java pid — filter ps by `coop bot/instance`.
- Bridge = external child of game JVM, reloaded only by game restart or user
  typing `\agent_bridge`. Programmatic `chat()` bypasses ChatScreenMixin.
- `java_access_field` returns object-table HANDLES — unbox via
  `java_to_string` before `int()` (containerId bug class).
- Baritone `#goto <block>` MINES the block (allowBreak default) — pathing
  now wraps allowBreak/allowPlace/allowParkourPlace false; explicit mine
  steps untouched. Don't revert without replacing container-loss protection.
- Menu slots (single chest): container 0–26, player main 27–53, hotbar 54–62.
  QUICK_MOVE moves whole stacks (can't split) — over-pull reported honestly.
- `state` auto-closes stray GUIs; raw `bridge.call("state")` doesn't.
  close_screen works on modded book screens (verified twice).
- Daemon binds 48750; bridge dials out and redials after disconnects.
  Probe scripts (`/tmp/opencode/ops.py,take.py,put.py`) need the daemon dead.
- Chat lines from bot bypass nothing; `.quit` in chat kills the bridge.
  Bot's own `[agent]` lines are filtered from its directive reading.
- `data/*` (base, session log) is runtime state, gitignored — back it up
  before wipes. Survey writes base_sp.json; daemon re-reads it every turn.
- Game window hidden by default; `--show` to watch. Bot inventory lives in
  HOST playerdata (CoopBot UUID) — safe across bot reinstalls, dies with
  their world backup discipline.
