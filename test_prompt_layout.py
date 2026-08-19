import re
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
PROJECT_DIR = PLUGIN_DIR.parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))
if getattr(sys.modules.get("astrbot"), "__path__", None) is None:
    sys.modules.pop("astrbot", None)
    sys.modules.pop("astrbot.api", None)

from astrbot_plugin_busy_schedule.main import _rebuild_system_prompt  # noqa: E402


def _block(prompt: str, name: str) -> str:
    match = re.search(
        rf"<!-- BUSY_SCHEDULE_{name} -->(.*?)<!-- /BUSY_SCHEDULE_{name} -->",
        prompt,
        re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def test_system_blocks_are_rebuilt_in_fixed_order_and_stale_blocks_removed():
    prompt = """
persona
<!-- BUSY_SCHEDULE_EXECUTION -->old exec<!-- /BUSY_SCHEDULE_EXECUTION -->
<!-- BUSY_SCHEDULE_BUSY -->old busy<!-- /BUSY_SCHEDULE_BUSY -->
<!-- EMOTION_STATE_ANCHOR -->
<!-- EMOTION_STATE_BEGIN -->old emotion<!-- /EMOTION_STATE_END -->
"""
    rebuilt = _rebuild_system_prompt(
        prompt,
        {
            "calendar": "calendar",
            "custom": "custom",
            "daily": "outfit\nweather\nschedule",
            "activity": "activity",
            "busy": "## 当前处于忙碌状态",
            "execution": "execution",
        },
    )

    positions = [
        rebuilt.index("BUSY_SCHEDULE_CALENDAR"),
        rebuilt.index("BUSY_SCHEDULE_CACHE"),
        rebuilt.index("BUSY_SCHEDULE_CUSTOM"),
        rebuilt.index("BUSY_SCHEDULE_ACTIVITY"),
        rebuilt.index("BUSY_SCHEDULE_BUSY"),
        rebuilt.index("EMOTION_STATE_ANCHOR"),
        rebuilt.index("EMOTION_STATE_BEGIN"),
        rebuilt.index("BUSY_SCHEDULE_EXECUTION"),
    ]
    assert positions == sorted(positions)
    assert rebuilt.count("BUSY_SCHEDULE_ACTIVITY") == 2
    assert "old exec" not in rebuilt
    assert "old busy" not in rebuilt
    assert "old emotion" in rebuilt


def test_missing_busy_or_execution_does_not_leave_stale_content():
    rebuilt = _rebuild_system_prompt(
        "persona\n<!-- BUSY_SCHEDULE_BUSY -->stale<!-- /BUSY_SCHEDULE_BUSY -->",
        {"daily": "daily"},
    )
    assert "stale" not in rebuilt
    assert rebuilt.index("BUSY_SCHEDULE_CACHE") < rebuilt.index("EMOTION_STATE_ANCHOR")
    assert "BUSY_SCHEDULE_EXECUTION" not in rebuilt


def test_changing_one_dynamic_block_keeps_the_other_blocks_identical():
    blocks = {
        "calendar": "calendar",
        "custom": "custom",
        "daily": "daily",
        "activity": "activity 1",
        "busy": "busy",
        "execution": "execution",
    }
    original = _rebuild_system_prompt("persona", blocks)
    updated = _rebuild_system_prompt(original, {**blocks, "activity": "activity 2"})

    assert _block(updated, "ACTIVITY") == "activity 2"
    for name in ("CALENDAR", "CUSTOM", "CACHE", "BUSY", "EXECUTION"):
        assert _block(updated, name) == _block(original, name)
        assert updated.count(f"BUSY_SCHEDULE_{name}") == 2
