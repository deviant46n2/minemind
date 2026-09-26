"""Provision the co-op bot's Minecraft client instance.

Bot instance lives at ./instance and mirrors your SubTerra pack (mods,
configs, kubejs) so it can join your LAN world as player "CoopBot".
Source of truth for pack content is your Prism instance:

    <flatpak>/instances/EMC SubTerra Tech/minecraft/

Also installs minescript + agent_bridge into BOTH your instance (needs a
client restart to take effect) and the bot instance.

    python3 tools/setup_instance.py [--fetch] [--skip-their-instance]
"""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTANCE = ROOT / "instance"
PRISM = Path.home() / ".local/share/PrismLauncher"
SRC_MC = (Path.home() / ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher"
          / "instances/EMC SubTerra Tech/minecraft")

MC_VERSION = "1.20.1"
FORGE_BUILD = "47.4.16"
FORGE_ART = f"{MC_VERSION}-{FORGE_BUILD}"
FORGE_ID = f"{MC_VERSION}-forge-{FORGE_BUILD}"
PY = "/usr/bin/python3"  # valid on host (3.14) and inside the flatpak runtime (3.13)

MINESCRIPT_JAR = (
    "minescript-forge-1.20.1-4.0.jar",
    "https://cdn.modrinth.com/data/KcpXWngB/versions/PWcYk7rH/"
    "minescript-forge-1.20.1-4.0.jar",
)
VERSION_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
FORGE_INSTALLER = (
    "https://maven.minecraftforge.net/net/minecraftforge/forge/"
    f"{FORGE_ART}/forge-{FORGE_ART}-installer.jar"
)

# Pack content to mirror. Everything else in their minecraft/ (saves, logs,
# backups, screenshots, options.txt) stays out of the bot instance.
SYNC = ["mods", "config", "kubejs", "defaultconfigs", "ESM", "ldlib", "local",
        "modernfix", "patchouli_books", "schematics", "skyblockbuilder"]
SYNC_FILES = ["patchouli_data.json", "rhino.local.properties"]

# Bot-client video settings: cheap, windowed, no shaders.
OPTIONS = """\
renderDistance:6
maxFps:30
fullscreen:false
enableVSync:false
autoJump:true
lang:en_us
fov:0
"""


def fetch(url: str, dest: Path) -> None:
    print(f"  GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "coopbot/0.1 (local)"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def ensure_dirs() -> None:
    for name in ("mods", "config", "saves", "minescript", "logs",
                 "resourcepacks", "shaderpacks", "crash-reports", "versions",
                 "libraries", "data"):
        (INSTANCE / name).mkdir(parents=True, exist_ok=True)


def link_assets() -> None:
    dst = INSTANCE / "assets"
    if not dst.exists():
        dst.symlink_to(PRISM / "assets", target_is_directory=True)



def ensure_libraries() -> None:
    """Download every library referenced by the vanilla + forge version jsons."""
    vdirs = sorted((INSTANCE / "versions").iterdir())
    jsons = []
    for d in vdirs:
        f = d / f"{d.name}.json"
        if f.exists():
            jsons.append(json.loads(f.read_text()))
    seen: set[str] = set()
    todo: list[tuple[str, str, Path]] = []
    MAVEN = ("https://maven.minecraftforge.net/",
             "https://libraries.minecraft.net/",
             "https://repo1.maven.org/maven2/",
             "https://maven.fabricmc.net/")
    for j in jsons:
        for lib in j.get("libraries", []):
            dl = lib.get("downloads") or {}
            arts = []
            if dl.get("artifact"):
                arts.append(dl["artifact"])
            arts.extend((dl.get("classifiers") or {}).values())
            for art in arts:
                path, url = art.get("path"), art.get("url") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                dest = INSTANCE / "libraries" / path
                if dest.exists():
                    continue
                if not url:
                    for base in MAVEN:
                        url = base + path
                        if http_ok(url):
                            break
                    else:
                        url = MAVEN[1] + path
                todo.append((path, url, dest))
    if not todo:
        print(f"all {len(seen)} libraries present")
        return
    print(f"downloading {len(todo)} missing libraries")
    for path, url, dest in todo:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            fetch(url, dest)
        except Exception as e:
            print(f"  ! {path}: {e}")
    missing = [t[0] for t in todo if not t[2].exists()]
    if missing:
        sys.exit(f"{len(missing)} libraries still missing, e.g. {missing[:3]}")


def http_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "coopbot/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def install_forge(do_fetch: bool) -> None:
    out = INSTANCE / "versions" / FORGE_ID / f"{FORGE_ID}.json"
    if out.exists():
        return
    vdir = INSTANCE / "versions" / MC_VERSION
    vjson = vdir / f"{MC_VERSION}.json"
    if not vjson.exists():
        if not do_fetch:
            sys.exit("vanilla version json missing; re-run with --fetch")
        vdir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(VERSION_MANIFEST) as r:
            manifest = json.load(r)
        url = next(v["url"] for v in manifest["versions"] if v["id"] == MC_VERSION)
        fetch(url, vjson)
        client = json.loads(vjson.read_text())["downloads"]["client"]
        fetch(client["url"], vdir / f"{MC_VERSION}.jar")

    profiles = INSTANCE / "launcher_profiles.json"
    if not profiles.exists():
        profiles.write_text(json.dumps(
            {"profiles": {}, "version": "2.0.0", "clientToken": "coopbot"}), encoding="utf-8")

    installer = ROOT / "vendor" / f"forge-{FORGE_ART}-installer.jar"
    installer.parent.mkdir(exist_ok=True)
    if not installer.exists():
        if not do_fetch:
            sys.exit("forge installer missing; re-run with --fetch")
        fetch(FORGE_INSTALLER, installer)
    print("running forge installer (downloads ~60 MB into instance/libraries)")
    res = subprocess.run(
        ["java", "-jar", str(installer), "--installClient", str(INSTANCE)],
        capture_output=True, text=True, cwd=INSTANCE,
    )
    print(res.stdout[-2000:] or res.stderr[-2000:])
    if not out.exists():
        sys.exit(f"forge install failed: {res.stdout[-500:]} {res.stderr[-500:]}")
    print(f"installed {FORGE_ART}")


def stage_forge_tools() -> None:
    """ForgeWrapper + forge installer jar + NewLaunch.jar (Prism launch path).

    launch.py starts org.prismlauncher.EntryPoint with mainClass=ForgeWrapper;
    ForgeWrapper re-reads forge's jvm args from the *installer* profile at
    instance/libraries/net/minecraftforge/forge/<art>/forge-<art>-installer.jar.
    """
    libs = INSTANCE / "libraries"
    # ForgeWrapper: copy every build Prism already has (source of truth).
    fw_src = (Path.home() / ".var/app/org.prismlauncher.PrismLauncher"
              / "data/PrismLauncher/libraries/io/github/zekerzhayard/ForgeWrapper")
    fw_dst = libs / "io/github/zekerzhayard/ForgeWrapper"
    if fw_src.exists():
        for vdir in fw_src.iterdir():
            jar = vdir / f"ForgeWrapper-{vdir.name}.jar"
            if jar.exists():
                (fw_dst / vdir.name).mkdir(parents=True, exist_ok=True)
                shutil.copy2(jar, fw_dst / vdir.name / jar.name)
    if not list(fw_dst.glob("*/ForgeWrapper-*.jar")):
        sys.exit("ForgeWrapper jar missing (expected in Prism libraries)")

    # forge installer jar = ForgeWrapper's config source (detector layout).
    inst = (libs / "net/minecraftforge/forge" / FORGE_ART
            / f"forge-{FORGE_ART}-installer.jar")
    if not inst.exists():
        inst.parent.mkdir(parents=True, exist_ok=True)
        vendored = ROOT / "vendor" / f"forge-{FORGE_ART}-installer.jar"
        if vendored.exists():
            shutil.copy2(vendored, inst)
        else:
            fetch(FORGE_INSTALLER, inst)

    # NewLaunch.jar: Prism's stdin-protocol EntryPoint.
    nl = ROOT / "tools" / "vendor" / "NewLaunch.jar"
    if not nl.exists():
        nl.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            ["flatpak", "run", "--command=cat", "org.prismlauncher.PrismLauncher",
             "/app/share/PrismLauncher/NewLaunch.jar"],
            capture_output=True)
        if res.returncode != 0 or res.stdout[:2] != b"PK":
            sys.exit("NewLaunch.jar missing and flatpak extraction failed")
        nl.write_bytes(res.stdout)
    print("launcher tools staged: ForgeWrapper + forge installer + NewLaunch.jar")


def sync_pack() -> None:
    if not SRC_MC.exists():
        sys.exit(f"pack instance not found: {SRC_MC}")
    for name in SYNC:
        src, dst = SRC_MC / name, INSTANCE / name
        if not src.exists():
            continue
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    for name in SYNC_FILES:
        src, dst = SRC_MC / name, INSTANCE / name
        if src.exists():
            shutil.copy2(src, dst)
    print(f"pack content synced from {SRC_MC}")


def stage_minescript(target_mods: Path) -> Path:
    jar, url = MINESCRIPT_JAR
    vendor = ROOT / "vendor" / jar
    if not vendor.exists():
        vendor.parent.mkdir(exist_ok=True)
        fetch(url, vendor)
    target_mods.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vendor, target_mods / jar)
    return target_mods / jar


def install_bridge(target_mc: Path) -> None:
    ms = target_mc / "minescript"
    ms.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "bridge" / "minescript" / "agent_bridge.py", ms / "agent_bridge.py")
    config = ms / "config.txt"
    if not config.exists():
        config.write_text(f'python="{PY}"\n', encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download missing vanilla/forge pieces")
    ap.add_argument("--skip-their-instance", action="store_true",
                    help="do not touch your own pack instance (bot instance only)")
    args = ap.parse_args()

    if not SRC_MC.exists():
        sys.exit(f"pack instance not found: {SRC_MC}")
    ensure_dirs()
    link_assets()
    install_forge(args.fetch)
    stage_forge_tools()
    sync_pack()
    stage_minescript(INSTANCE / "mods")
    install_bridge(INSTANCE)
    ensure_libraries()
    if not (INSTANCE / "options.txt").exists():
        (INSTANCE / "options.txt").write_text(OPTIONS, encoding="utf-8")

    if not args.skip_their_instance:
        stage_minescript(SRC_MC / "mods")
        install_bridge(SRC_MC)
        print(f"minescript+bridge added to your instance (restart required): {SRC_MC}")

    mods = sorted(p.name for p in (INSTANCE / "mods").glob("*.jar"))
    print(f"bot instance ready: {INSTANCE} ({len(mods)} mods)")


if __name__ == "__main__":
    main()
