"""Launch argv/stdin contract for tools/launch.py (Prism stdin protocol).

Run: python3 test/test_launch.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCH = ROOT / "tools" / "launch.py"


def run(*args: str) -> str:
    r = subprocess.run([sys.executable, str(LAUNCH), "--dry-run", *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"exit {r.returncode}: {r.stderr[-800:]}"
    return r.stdout


def test_entrypoint_protocol() -> None:
    out = run()
    assert "org.prismlauncher.EntryPoint" in out, "must launch via NewLaunch EntryPoint"
    assert "-Djava.library.path=" in out, "natives dir missing"
    assert "ForgeWrapper" in out, "ForgeWrapper jar not on classpath"
    assert "io.github.zekerzhayard.forgewrapper.installer.Main" in out, \
        "stdin mainClass missing"
    assert "param --fml.forgeVersion\nparam 47.4.16\n" in out + "\n", \
        "forge game args not forwarded (one param per argv element)"
    assert out.rstrip().endswith("launch"), "stdin must terminate with launch sentinel"
    # frame 1: no -p / --add-modules on the JVM command line (ForgeWrapper
    # installs the module path at runtime; a static -p duplicates 'minecraft')
    head = out.split("--- stdin ---")[0]
    assert " -p " not in head and "--add-modules" not in head, \
        "static module-path args must not be passed"
    assert "BootstrapLauncher" not in head.split("\n")[0], \
        "main process must be EntryPoint, not BootstrapLauncher"


def test_join_game_args() -> None:
    out = run("--join", "127.0.0.1:37875")
    assert "param --quickPlayMultiplayer\nparam 127.0.0.1:37875\n" in out + "\n", \
        "quickPlay join arg missing from stdin params"


def main() -> None:
    test_entrypoint_protocol()
    print("ok  test_entrypoint_protocol")
    test_join_game_args()
    print("ok  test_join_game_args")
    print("PASS")


if __name__ == "__main__":
    main()
