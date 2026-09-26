# CoopBot — AI co-op companion for modded Minecraft

**A fork of [MineMind](https://github.com/Boyan253/minemind) that joins your
world as a real second player.**

CoopBot launches its own Minecraft client, joins your single-player world over
LAN as the player **CoopBot**, reads your pack's FTB Quests book, plans with a
local LLM, and works the quest frontier alongside you — mining, checking off
quests, requesting crafting help in chat, recovering from failure.

Built and verified against **EMC SubTerra Tech** (CurseForge · MC 1.20.1 ·
Forge 47.4.16 · 204 mods · 645 quests), but the quest/recipe plumbing is
pack-agnostic (any FTB Quests pack).

```
┌─ your PC ────────────────────────────────────────────────────────────┐
│                                                                      │
│  daemon.py  ──plans──▶  LLM (local llama-server / API)               │
│    │  ▲                                                              │
│    │  └── quest_graph.json  (built offline from the pack's           │
│    │       FTB Quests files — full dependency DAG)                   │
│    ▼                                                                 │
│  TCP 127.0.0.1:48750  (JSON lines request/response)                  │
│    ▲                                                                 │
│ ┌──┴────────────── CoopBot's Minecraft client ────────────────────┐  │
│ │ Minescript + agent_bridge  ── chat/state/inventory I/O          │  │
│ │ (joined over LAN via mcwifipnp offline-auth, op level 2)        │  │
│ └─────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

The LLM never clicks pixels and never types keystrokes. Perception is files
and registries (quest DAG, save state, live inventory); action is deterministic
bridge commands. The model only decides **what to do next**.

## What works (verified, not aspirational)

- [x] **Join**: own client joins your open world over LAN. OpenToLAN + mcwifipnp
  forces offline auth so the non-Mojang profile connects; LAN port 25565.
- [x] **Bridge**: Minescript `agent_bridge` autoruns at world join
  (`[*]=\agent_bridge`), reconnects if the daemon restarts (tested).
- [x] **Quest graph**: `tools/build_quest_graph.py` → `data/quest_graph.json`
  (22 chapters, 645 quests, dependency DAG, frontier computation).
- [x] **Local planning**: Qwen3-14B (llama-server, OpenAI-compatible) emits
  valid multi-step quest plans.
- [x] **quest_checkmark full chain** — daemon → bridge → `/ftbquests
  change_progress @s complete <id>` → server accepted → progress flushed to
  the world save and read back by the frontier logic. Requires **op level ≥ 2**
  (see Gotchas — this was the one real bug and it was environmental).
- [x] **Failure handling**: rejected commands / timeouts → retry once →
  replan; logs every step result.
- [x] **Human-assist escalation**: crafting/smelting waits for you to type
  `.done` in chat instead of silently failing.
- [x] **Tests**: `test/` covers launch contract, bridge reconnect, quest-graph
  build, local-LLM provider selection — all passing.
- [ ] Movement/inventory actions exercised end-to-end in a daemon run
  (implemented upstream; `container_click_slot` bridge capability is missing).
- [ ] Quest reward auto-claiming, machine GUI automation.

## Quick start

Requirements: Python 3.10+, a Forge 1.20.1 instance with Minescript + mcwifipnp,
Java 17, and either a local llama-server or an API key.

```bash
export AGENT_MC_DIR="/path/to/instance/minecraft"

# 1. build the quest graph from your pack
python3 tools/build_quest_graph.py

# 2. start your world, open it to LAN (mcwifipnp keeps it offline-authable)
# 3. launch the companion client and wait until it has joined
python3 tools/launch.py --join 127.0.0.1:25565 --heap 6G --wait --shot joined

# 4. grant it editor permission (once per world — see Gotchas)
#    in your in-game chat:  /op CoopBot

# 5. run the brain (the bridge autoruns when the bot joins; if the daemon
#    starts first it simply waits for the connection)
export LOCAL_LLM_URL=http://127.0.0.1:8081/v1/chat/completions
export LOCAL_LLM_MODEL=qwen3-14b
python3 agent/daemon.py --max-cycles 3 --body player
```

Useful flags:

- `--body player` — the bot's own body (CoopBot) executes steps (default
  `companion` spawns a PlayerEngine NPC instead).
- `--plan plan.json` — run one fixed plan and exit (deterministic repro).
- `--mp` — dedicated-server mode: progress ledger instead of reading saves.
- Chat steering in-game: `.goal get me …`, `.goal clear`, `#stop`.

Run the tests:

```bash
for t in test/test_*.py; do python3 "$t"; done
```

## Gotchas (learned the hard way)

- **`/ftbquests change_progress` is op-gated (level ≥ 2).** Without it the
  brigadier parser stops after `ftbquests` and vanilla reports the error with
  `<--[HERE]` at the very end of the input — which looks exactly like a
  malformed command. Run `/op CoopBot` once and the same command returns
  `Progress has been changed!`. The command syntax itself was always correct.
- **Never kill the game by process name.** Your own client and the bot share
  `java`; kill only PIDs whose cmdline contains the bot instance path.
- **LAN port**: mcwifipnp publishes on 25565; the bot client passes
  keyboard input unreliably, so the bridge is wired as an autorun instead.
- **World save is shared**: don't disk-edit `saves/` while your client has the
  world open; reads are fine.
- **Crafting is intentionally human-assisted**: the bridge lacks
  `container_click_slot`, so `craft`/`smelt` steps ask you in chat and wait
  for `.done` (15 min timeout, then replan).

## Layout

| Path | What |
|---|---|
| `agent/` | daemon, planner prompts, actions, world/quest state |
| `bridge/minescript/agent_bridge.py` | in-game bridge (copied into the instance by `tools/install_bridge.py`) |
| `tools/` | launch client, build quest graph, setup instance |
| `data/` | quest graph + session logs (generated, gitignored) |
| `instance/` | the bot's Minecraft instance (generated, gitignored) |
| `test/` | runnable verification scripts (`python3 test/…`) |

## Credits & license

Forked from [MineMind](https://github.com/Boyan253/minemind) (MIT). MineMind
stands on [PlayerEngine](https://github.com/Goodbird-git/PlayerEngine) ·
[Automatone](https://github.com/Ladysnake/Automatone) ·
[AltoClef](https://github.com/gaucho-matrero/altoclef) ·
[Baritone](https://github.com/cabaletta/baritone) ·
[Minescript](https://minescript.net) ·
[FTB Quests](https://github.com/FTBTeam/FTB-Quests) · mcwifipnp. Mod jars are
**not** bundled.
