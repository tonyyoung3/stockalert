import os


def harness_enabled() -> bool:
    return os.environ.get("HARNESS_ENABLED", "").strip().lower() in {"1", "true", "yes"}
