def is_usable_assistant_response(response) -> bool:
    """Return whether a main Agent response should refresh chat protection."""
    role = str(getattr(response, "role", "") or "").lower()
    completion_text = str(getattr(response, "completion_text", "") or "").strip()
    return role == "assistant" and bool(completion_text)


def is_natural_spark_proactive(event) -> bool:
    """Return whether this is a naturally scheduled Spark proactive request."""
    return bool(event.get_extra("spark_proactive_retrieval", False)) and not bool(
        event.get_extra("spark_slash_triggered", False)
    )
