"""Launch the co-op bot's Minecraft client (Forge 1.20.1) as player CoopBot.

Builds the classpath from instance/versions/<forge>/<forge>.json (+ vanilla
parent), extracts natives, then launches exactly like Prism does:

    java ... -cp <cp>:ForgeWrapper:NewLaunch.jar org.prismlauncher.EntryPoint

and feeds Prism's stdin protocol with mainClass = ForgeWrapper. ForgeWrapper
installs forge's module path / --add-opens at RUNTIME; invoking
BootstrapLauncher directly with those as static JVM args makes FML resolve
two modules named "minecraft" and crash.

    python3 tools/launch.py [--join localhost:25565] [--username CoopBot]
                            [--heap 4G] [--dry-run] [--wait] [--shot NAME]
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTANCE = ROOT / "instance"
JAVA = os.environ.get("COOPBOT_JAVA") or "/usr/lib/jvm/java-17-openjdk/bin/java"
VERSION = "1.20.1-forge-47.4.16"
OS_NAME, OS_ARCH = "linux", "x86_64"
USERNAME = "CoopBot"
PROOF_DIR = ROOT / "data" / "proof"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def rules_ok(rules, features: dict) -> bool:
    if not rules:
        return True
    allowed = False
    for rule in rules:
        os_rule, feat = rule.get("os") or {}, rule.get("features") or {}
        match = True
        if os_rule.get("name") and os_rule["name"] != OS_NAME:
            match = False
        if os_rule.get("arch") and os_rule["arch"] != OS_ARCH:
            match = False
        if any(features.get(k, False) != v for k, v in feat.items()):
            match = False
        if match:
            allowed = rule.get("action") == "allow"
    return allowed


def maven_path(coord: str) -> str:
    """net.example:artifact:1.0[:classifier][@ext] -> net/example/artifact/1.0/..."""
    if "@" in coord:
        coord, ext = coord.split("@", 1)
    else:
        ext = "jar"
    parts = coord.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    tail = f"-{parts[3]}" if len(parts) > 3 else ""
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{tail}.{ext}"


def lib_path(lib: dict) -> Path | None:
    art = (lib.get("downloads") or {}).get("artifact")
    if art and art.get("path"):
        return INSTANCE / "libraries" / art["path"]
    if lib.get("natives"):  # legacy layout: classifiers map
        classifiers = (lib.get("downloads") or {}).get("classifiers") or {}
        key = lib["natives"].get(OS_NAME)
        if key and key in classifiers:
            return INSTANCE / "libraries" / classifiers[key]["path"]
        return None
    if "name" in lib:
        return INSTANCE / "libraries" / maven_path(lib["name"])
    return None


def substitute(args: list, subs: dict) -> list:
    out = []
    for a in args:
        if isinstance(a, dict):  # {"rules": [...], "value": ...}
            if not rules_ok(a.get("rules"), FEATURES):
                continue
            out.extend(substitute([a["value"]], subs))
        elif isinstance(a, list):
            out.extend(substitute(a, subs))
        else:
            for k, v in subs.items():
                a = a.replace(k, v)
            out.append(a)
    unresolved = [a for a in out if isinstance(a, str) and "${" in a]
    if unresolved:
        print(f"  ! unresolved placeholders: {unresolved}", file=sys.stderr)
    return out


FEATURES: dict = {}


def build() -> tuple[list, dict]:
    child = load(INSTANCE / "versions" / VERSION / f"{VERSION}.json")
    parent = load(INSTANCE / "versions" / child["inheritsFrom"] / f"{child['inheritsFrom']}.json")

    cp: list[Path] = []
    for lib in child["libraries"] + parent["libraries"]:
        if not rules_ok(lib.get("rules"), FEATURES):
            continue
        p = lib_path(lib)
        if p and p.exists() and p not in cp:
            cp.append(p)
    # FML registers the game jar as module "minecraft"; a classpath copy named
    # "1.20.1.jar" derives automatic module name "_1._20._1" and collides.
    # Name it minecraft-1.20.1-client.jar (official/Prism layout) so both agree.
    client = INSTANCE / "libraries" / "com" / "mojang" / "minecraft" / parent["id"] \
        / f"minecraft-{parent['id']}-client.jar"
    if not client.exists():
        src = INSTANCE / "versions" / parent["id"] / f"{parent['id']}.jar"
        if not src.exists():
            sys.exit(f"missing client jar {client} or {src} — re-run setup_instance.py --fetch")
        client.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, client)
    cp.append(client)

    natives = INSTANCE / "natives"
    natives.mkdir(exist_ok=True)
    if not any(natives.iterdir()):
        for p in cp:
            if "natives" in p.name and p.suffix == ".jar":
                with zipfile.ZipFile(p) as z:
                    for n in z.namelist():
                        if "/" not in n.rstrip("/") or not n.endswith((".so", ".dll", ".dylib")):
                            if n.endswith(".so"):
                                (natives / Path(n).name).write_bytes(z.read(n))
        print(f"extracted natives -> {natives}")

    subs = {
        "${library_directory}": str(INSTANCE / "libraries"),
        "${classpath_separator}": os.pathsep,
        "${version_name}": VERSION,
        "${natives_directory}": str(natives),
        "${classpath}": os.pathsep.join(str(p) for p in cp),
        "${launcher_name}": "coopbot",
        "${launcher_version}": "1.0",
        "${auth_player_name}": ARGS.username,
        "${auth_uuid}": ARGS.uuid,
        "${auth_access_token}": "0",
        "${clientid}": "0",
        "${auth_xuid}": "0",
        "${user_type}": "legacy",
        "${user_properties}": "{}",
        "${game_directory}": str(INSTANCE),
        "${game_assets}": str(INSTANCE / "assets" / "virtual" / "legacy"),
        "${assets_root}": str(INSTANCE / "assets"),
        "${assets_index_name}": parent.get("assetIndex", {}).get("id", ""),
        "${version_type}": parent.get("type", "release"),
        "${resolution_width}": "1280",
        "${resolution_height}": "720",
        "${quickPlayMultiplayer}": ARGS.join or "-",
        "${quickPlaySingleplayer}": ARGS.singleplayer or "-",
        "${quickPlayDemo}": "-",
        "${demo_user_name}": "-",
    }
    if ARGS.join:
        FEATURES.update({"has_quick_play_support": True,
                         "is_quick_play_multiplayer": True})
    if ARGS.singleplayer:
        FEATURES.update({"has_quick_play_support": True,
                         "is_quick_play_singleplayer": True})

    game = substitute(parent.get("arguments", {}).get("game", []), subs)
    game += substitute(child.get("arguments", {}).get("game", []), subs)

    # ForgeWrapper + NewLaunch: Prism's launcher pieces. ForgeWrapper re-reads
    # forge's jvm args from the installer profile and applies -p/--add-opens
    # reflectively at runtime (Bootstrap.setupBootstrapLauncher / ModuleUtil).
    fw = sorted((INSTANCE / "libraries" / "io" / "github" / "zekerzhayard"
                 / "ForgeWrapper").glob("*/ForgeWrapper-*.jar"))
    if not fw:
        sys.exit("ForgeWrapper jar missing — run tools/setup_instance.py")
    nl = ensure_newlaunch()
    cp.append(fw[-1])
    cp.append(nl)

    argv = [
        JAVA, "-Duser.language=en", "-Xms512m", f"-Xmx{ARGS.heap}",
        f"-Djava.library.path={natives}",
        "-cp", os.pathsep.join(str(p) for p in cp),
        "org.prismlauncher.EntryPoint",
    ]
    return argv, game, subs


def ensure_newlaunch() -> Path:
    dest = ROOT / "tools" / "vendor" / "NewLaunch.jar"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["flatpak", "run", "--command=cat", "org.prismlauncher.PrismLauncher",
         "/app/share/PrismLauncher/NewLaunch.jar"],
        capture_output=True)
    if res.returncode == 0 and res.stdout[:2] == b"PK":
        dest.write_bytes(res.stdout)
        return dest
    sys.exit("NewLaunch.jar missing and flatpak extraction failed "
             "(expected at tools/vendor/NewLaunch.jar)")


def stdin_params(game: list[str]) -> str:
    lines = [
        "launcherBrand coopbot",
        "launcherVersion 1.0",
        "launcher standard",
        "mainClass io.github.zekerzhayard.forgewrapper.installer.Main",
        "windowParams 1280x720",
        "instanceName coopbot",
    ]
    lines += [f"param {a}" for a in game]
    lines.append("launch")
    return "\n".join(lines) + "\n"


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock: socket.socket) -> int:
    n = shift = 0
    while True:
        raw = sock.recv(1)
        if not raw:
            raise OSError("eof")
        n |= (raw[0] & 0x7F) << shift
        if not raw[0] & 0x80:
            return n
        shift += 7


def ping_status(host: str, port: int, timeout: float = 3.0) -> dict | None:
    """MC server-list ping (status state, protocol 763) -> parsed JSON status."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            hs = (b"\x00" + _varint(763) + _varint(len(host)) + host.encode()
                  + port.to_bytes(2, "big") + _varint(1))
            s.sendall(_varint(len(hs)) + hs + _varint(1) + _varint(0))
            _read_varint(s)
            _read_varint(s)
            length, data = _read_varint(s), b""
            while len(data) < length:
                chunk = s.recv(length - len(data))
                if not chunk:
                    return None
                data += chunk
            return json.loads(data)
    except OSError:
        return None


def bot_online(join: str) -> bool:
    """True when the LAN server's player list contains CoopBot."""
    host, _, port = join.rpartition(":")
    status = ping_status(host or "127.0.0.1", int(port))
    if not status:
        return False
    names = [p.get("name", "") for p in (status.get("players") or {}).get("sample") or []]
    return "CoopBot" in names or (status.get("players") or {}).get("online", 0) >= 2


def wait_ready(log: Path, proc: subprocess.Popen, timeout: float = 420) -> bool:
    # No join: title screen = ready. With --join: only the server seeing
    # CoopBot counts — a title screen proves nothing about the LAN session.
    markers = () if ARGS.join else ("Sound engine started", "Narrator library")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"client exited early with code {proc.returncode}")
            return False
        if ARGS.join and bot_online(ARGS.join):
            return True
        if log.exists() and any(m in log.read_text(errors="replace") for m in markers):
            return True
        time.sleep(2)
    return False


def bot_window(pid: int) -> str | None:
    """Client window id of the bot's game window, or None without xdotool."""
    if not shutil.which("xdotool"):
        return None
    res = subprocess.run(["xdotool", "search", "--pid", str(pid)],
                         capture_output=True, text=True)
    ids = res.stdout.split()
    return ids[-1] if ids else None


def _x(*args: str) -> None:
    subprocess.run(["xdotool", *args], check=False, capture_output=True)


def _active_window() -> str | None:
    res = subprocess.run(["xdotool", "getactivewindow"],
                         capture_output=True, text=True)
    return res.stdout.strip() or None


_focus_guard_stop: threading.Event | None = None


def set_hidden(pid: int, hidden: bool) -> bool:
    """Unmap (or re-map) the bot's game window.

    The bot never needs focus -- it acts entirely in-process -- so its window
    must not sit on the human's screen. Unmapping keeps the client running
    (verified: state + movement ops still work) while leaving exactly one
    visible game window.

    Re-mapping makes the window manager hand the new window focus, which
    would open the *human's* Game Menu (their pauseOnLostFocus is on). A
    single windowfocus call loses the race against the WM's own
    focus-follows-map, so a guard keeps pulling focus off the bot window for
    as long as it stays mapped.
    """
    global _focus_guard_stop
    win = bot_window(pid)
    if not win:
        return False
    if hidden:
        _x("windowunmap", win)
        if _focus_guard_stop is not None:
            _focus_guard_stop.set()
            _focus_guard_stop = None
        return True
    prev = _active_window()
    _x("windowmap", win)
    stop = _focus_guard_stop = threading.Event()

    def guard() -> None:
        while not stop.is_set():
            if _active_window() == win:
                _x("windowfocus", prev)  # no --sync: never block here
            stop.wait(0.03)

    threading.Thread(target=guard, daemon=True).start()
    if prev and prev != win:
        _x("windowfocus", "--sync", prev)
    return True


def shot(name: str, pid: int | None = None) -> Path:
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    out = PROOF_DIR / f"{name}.png"
    # grim captures the screen, so a hidden bot window would silently be
    # missing from the proof: show it for the capture, hide it again.
    shown = False
    if pid and bot_window(pid):
        visible = subprocess.run(["xdotool", "search", "--onlyvisible",
                                  "--pid", str(pid)],
                                 capture_output=True, text=True).stdout.split()
        if not visible:
            set_hidden(pid, False)
            shown = True
            time.sleep(0.6)
    subprocess.run(["grim", str(out)], check=False)
    if shown:
        set_hidden(pid, True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", default=USERNAME)
    ap.add_argument("--uuid", default="00000000-0000-0000-0000-0000c00b07b0")
    ap.add_argument("--heap", default="4G")
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--join", default=None,
                        help="auto-join host:port (LAN world)")
    target.add_argument("--singleplayer", default=None, metavar="WORLD",
                        help="open WORLD from the bot's own saves/ instead of "
                             "joining — the autonomous test bed, no human host needed")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait", action="store_true", help="poll log until in-game")
    ap.add_argument("--shot", default=None, help="take grim screenshot into data/proof/")
    ap.add_argument("--show", action="store_true",
                    help="keep the bot's game window on screen (default: hide it)")
    globals()["ARGS"] = ap.parse_args()

    argv, game, _ = build()
    params = stdin_params(game)
    if ARGS.dry_run:
        print("\n".join(argv))
        print("--- stdin ---")
        print(params, end="")
        return
    print(" ".join(argv[:6]), "...", " ".join(argv[-6:]))

    log = INSTANCE / "logs" / "latest.log"
    log.unlink(missing_ok=True)
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1"))
    with open(INSTANCE / "logs" / "launcher.log", "a") as f:
        proc = subprocess.Popen(argv, cwd=INSTANCE, stdin=subprocess.PIPE,
                                stdout=f, stderr=subprocess.STDOUT, env=env,
                                text=True)
    assert proc.stdin is not None
    proc.stdin.write(params)
    proc.stdin.close()
    print(f"launched pid {proc.pid}")
    if ARGS.wait:
        ok = wait_ready(log, proc)
        print("in-game" if ok else "NOT ready")
        if ARGS.join:
            host, _, port = ARGS.join.rpartition(":")
            status = ping_status(host or "127.0.0.1", int(port)) or {}
            players = [p.get("name") for p in
                       (status.get("players") or {}).get("sample") or []]
            print(f"server {ARGS.join}: online="
                  f"{(status.get('players') or {}).get('online')} players={players}")
        if ok and not ARGS.show and set_hidden(proc.pid, True):
            print("bot window hidden (--show to keep it visible)")
        if ok and ARGS.shot:
            print(f"proof: {shot(ARGS.shot, proc.pid)}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
