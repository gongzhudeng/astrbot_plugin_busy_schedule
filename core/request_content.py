"""Provider request helpers for non-persistent busy-schedule context."""

from astrbot.core.agent.message import ContentPart, TextPart
from astrbot.core.provider.entities import ProviderRequest

_TEMP_BLOCK_START = "<!-- BUSY_SCHEDULE_TEMP_USER_BEGIN -->"
_TEMP_BLOCK_END = "<!-- /BUSY_SCHEDULE_TEMP_USER_END -->"


def _part_text(part: ContentPart | dict) -> str:
    if isinstance(part, dict):
        return str(part.get("text", ""))
    return str(getattr(part, "text", ""))


def _is_busy_schedule_temp_part(part: ContentPart | dict) -> bool:
    text = _part_text(part)
    return _TEMP_BLOCK_START in text and _TEMP_BLOCK_END in text


def build_temp_user_content(*blocks: str) -> str:
    """Build one ordered provider-only block from non-empty sections."""
    content = "\n\n".join(block.strip() for block in blocks if block and block.strip())
    if not content:
        return ""
    return f"{_TEMP_BLOCK_START}\n{content}\n{_TEMP_BLOCK_END}"


def replace_temp_user_content(req: ProviderRequest, *blocks: str) -> None:
    """Replace this plugin's temporary tail while preserving other attachments."""
    req.extra_user_content_parts[:] = [
        part
        for part in req.extra_user_content_parts
        if not _is_busy_schedule_temp_part(part)
    ]
    content = build_temp_user_content(*blocks)
    if content:
        req.extra_user_content_parts.append(TextPart(text=content).mark_as_temp())
