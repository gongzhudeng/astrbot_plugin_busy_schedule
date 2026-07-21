from importlib import import_module

_EXPORTS = {
    "ScheduleDataManager": (".data", "ScheduleDataManager"),
    "ScheduleGenerator": (".generator", "ScheduleGenerator"),
    "BusyPeriodManager": (".busy_manager", "BusyPeriodManager"),
    "MessageInterceptor": (".message_interceptor", "MessageInterceptor"),
    "PromptInjector": (".prompt_injector", "PromptInjector"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
