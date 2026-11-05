"""Browser tests need the optional `e2e` group and an installed Chromium; skip cleanly otherwise.

Fixtures are module-scoped on purpose: Playwright's sync API keeps an asyncio loop running while it
is active, which would break later tests that call ``asyncio.run`` if it stayed open all session.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.browser


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/demo/browser" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.browser)


@pytest.fixture(scope="module")
def playwright_instance() -> Iterator[Any]:
    sync_api = pytest.importorskip(
        "playwright.sync_api", reason="install with: uv sync --group e2e"
    )
    with sync_api.sync_playwright() as playwright:
        yield playwright


def chromium_env() -> dict[str, str]:
    """Add the optional user-space libnss/libnspr directory (scripts/fetch-chromium-libs.sh)."""
    root = Path(os.environ.get("RIVA_CHROMIUM_LIBS_ROOT", "~/.cache/riva-chromium-libs"))
    libs = root.expanduser() / "extracted" / "usr" / "lib" / "x86_64-linux-gnu"
    env = dict(os.environ)
    if libs.is_dir():
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            part for part in (str(libs), env.get("LD_LIBRARY_PATH", "")) if part
        )
    return env


@pytest.fixture(scope="module")
def chromium(playwright_instance: Any) -> Iterator[Any]:
    try:
        browser = playwright_instance.chromium.launch(headless=True, env=chromium_env())
    except Exception as error:  # noqa: BLE001 - any launch failure means "not installed"
        pytest.skip(
            f"Chromium unavailable ({type(error).__name__}); run `playwright install chromium` "
            "and, if libnss3 is missing, scripts/fetch-chromium-libs.sh"
        )
    yield browser
    browser.close()
