"""build_quest_graph.py: build from AGENT_MC_DIR instance (SubTerra pack).

Run: python3 test/test_build_quest_graph.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "build_quest_graph.py"

CHAPTER_SNBT = """{
  id: "CHAPTER00000001"
  filename: "first_steps"
  title: "{ftbquests.first_steps}"
  order_index: 0
  quests: [
    {
      id: "AABBCCDD00112233"
      title: ""
      description: []
      dependencies: []
      tasks: [ { type: "item", id: "T1", item: "minecraft:stone", count: 16 } ]
      rewards: []
    },
    {
      id: "AABBCCDD00112244"
      title: "Second Step"
      description: []
      dependencies: [ "AABBCCDD00112233" ]
      tasks: [ { type: "item", id: "T2", item: "minecraft:dirt", count: 1 } ]
      rewards: []
    }
  ]
}
"""


def test_build_from_agent_mc_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mc = Path(tmp) / "minecraft"
        quests = mc / "config" / "ftbquests" / "quests" / "chapters"
        quests.mkdir(parents=True)
        (quests / "first_steps.snbt").write_text(CHAPTER_SNBT, encoding="utf-8")
        lang = mc / "kubejs" / "assets" / "ftbquestlocalizer" / "lang"
        lang.mkdir(parents=True)
        (lang / "en_us.json").write_text(
            json.dumps({"ftbquests.first_steps": "&#FF0000First &lSteps"}),
            encoding="utf-8")
        out = Path(tmp) / "quest_graph.json"

        env = dict(os.environ, AGENT_MC_DIR=str(mc), AGENT_QUEST_GRAPH_OUT=str(out))
        r = subprocess.run([sys.executable, str(TOOL)], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr[-800:]}"
        assert out.exists(), "quest_graph.json not written to AGENT_QUEST_GRAPH_OUT"
        graph = json.loads(out.read_text(encoding="utf-8"))

        assert len(graph["chapters"]) == 1, graph.keys()
        ch = graph["chapters"][0]
        assert ch["title"] == "First Steps", f"lang not resolved: {ch['title']!r}"
        assert len(ch["quests"]) == 2
        q1, q2 = ch["quests"]
        # empty title falls back to the first task's item name
        assert q1["title"] == "Stone", f"fallback title: {q1['title']!r}"
        assert q1["id"] == "AABBCCDD00112233", "quest id case must be preserved"
        assert q1["tasks"][0]["item"] == "minecraft:stone"
        assert q1["tasks"][0]["count"] == 16
        assert q2["title"] == "Second Step"
        assert q2["dependencies"] == ["AABBCCDD00112233"]
        s = graph["stats"]
        assert s["quests"] == 2 and s["dependency_edges"] == 1
        assert s["root_quests"] == 1, s
        assert not s["has_cycle"], s
        assert not s["dangling_dependencies"], s


def test_missing_lang_file_is_not_fatal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mc = Path(tmp) / "minecraft"
        quests = mc / "config" / "ftbquests" / "quests" / "chapters"
        quests.mkdir(parents=True)
        (quests / "c.snbt").write_text(CHAPTER_SNBT, encoding="utf-8")
        out = Path(tmp) / "g.json"
        env = dict(os.environ, AGENT_MC_DIR=str(mc), AGENT_QUEST_GRAPH_OUT=str(out))
        r = subprocess.run([sys.executable, str(TOOL)], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"exit {r.returncode}: {r.stderr[-800:]}"
        graph = json.loads(out.read_text(encoding="utf-8"))
        # no lang file: key stays literal instead of crashing
        assert graph["chapters"][0]["title"] == "{ftbquests.first_steps}"


def main() -> None:
    test_build_from_agent_mc_dir()
    print("ok  test_build_from_agent_mc_dir")
    test_missing_lang_file_is_not_fatal()
    print("ok  test_missing_lang_file_is_not_fatal")
    print("PASS")


if __name__ == "__main__":
    main()
