import os

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("RIVA_RUN_LIVE_TESTS") == "1":
        return
    for item in items:
        if "live" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="Set RIVA_RUN_LIVE_TESTS=1 to opt in"))
