import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
PROJECT_DIR = PLUGIN_DIR.parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if getattr(sys.modules.get("astrbot"), "__path__", None) is None:
    sys.modules.pop("astrbot", None)
    sys.modules.pop("astrbot.api", None)

from core.request_content import (  # noqa: E402
    build_temp_user_content,
    replace_temp_user_content,
)

from astrbot.core.agent.message import (  # noqa: E402
    Message,
    TextPart,
    dump_messages_with_checkpoints,
)
from astrbot.core.provider.entities import ProviderRequest  # noqa: E402


def test_busy_schedule_tail_is_one_temp_user_part_in_stable_order():
    request = ProviderRequest(
        system_prompt="stable system prompt",
        extra_user_content_parts=[TextPart(text="existing attachment")],
    )

    replace_temp_user_content(request, "schedule", "activity", "wake explanation")

    assert request.system_prompt == "stable system prompt"
    assert [part.text for part in request.extra_user_content_parts] == [
        "existing attachment",
        build_temp_user_content("schedule", "activity", "wake explanation"),
    ]
    temp_part = request.extra_user_content_parts[-1]
    assert temp_part._no_save is True
    assert temp_part.model_dump_for_context()["_no_save"] is True


def test_replacing_busy_schedule_tail_does_not_duplicate_dynamic_content():
    request = ProviderRequest()

    replace_temp_user_content(request, "old activity")
    replace_temp_user_content(request, "new activity")

    assert len(request.extra_user_content_parts) == 1
    assert request.extra_user_content_parts[0].text.endswith(
        "new activity\n<!-- /BUSY_SCHEDULE_TEMP_USER_END -->"
    )
    assert "old activity" not in request.extra_user_content_parts[0].text


def test_temp_tail_is_removed_from_persisted_user_history():
    temp_part = TextPart(
        text=build_temp_user_content("wake explanation")
    ).mark_as_temp()
    message = Message(
        role="user",
        content=[TextPart(text="real queued message"), temp_part],
    )

    saved = dump_messages_with_checkpoints([message])

    assert saved == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "real queued message"},
            ],
        }
    ]
