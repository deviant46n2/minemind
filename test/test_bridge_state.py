"""op_state must tolerate minescript functions that exist but return None.

The bridge docstring promises 'returns null in state instead of crashing';
live runs showed `TypeError: cannot unpack non-iterable NoneType object`
right after join because `x, y, z = pos()` unpacks a None position."""
import importlib.util
import sys
import types
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "minescript" / "agent_bridge.py"


def load_bridge():
    fake = types.ModuleType("minescript")
    # functions exist but transiently return None (observed post-join)
    fake.player_name = lambda: None
    fake.player_position = lambda: None
    fake.player_health = lambda: None
    fake.player_inventory = lambda: None
    fake.world_info = lambda: None
    sys.modules["minescript"] = fake
    spec = importlib.util.spec_from_file_location("agent_bridge_state_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_state_survives_none_results():
    mod = load_bridge()
    out = mod.op_state({})
    assert isinstance(out, dict), f"op_state should return a dict, got {out!r}"
    assert "position" not in out, f"None position must be omitted, got {out.get('position')!r}"


if __name__ == "__main__":
    test_state_survives_none_results()
    print("PASS test_state_survives_none_results")
