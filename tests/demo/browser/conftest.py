"""Browser tests need the optional `e2e` group and an installed Chromium; skip cleanly otherwise.

Fixtures are module-scoped on purpose: Playwright's sync API keeps an asyncio loop running while it
is active, which would break later tests that call ``asyncio.run`` if it stayed open all session.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
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


ASSET_TYPES = {".js": "application/javascript", ".wasm": "application/wasm"}


def _asset_root() -> Path:
    return Path(
        os.environ.get("RIVA_BROWSER_ASSETS", "~/.cache/riva-browser-test-assets")
    ).expanduser()


def serve_vad_assets(context: Any) -> None:
    """Serve the pinned jsDelivr VAD/ONNX files from the local cache, so tests need no internet."""
    root = _asset_root()
    if not (root / "@ricky0123").is_dir():
        pytest.skip("VAD assets missing; run: uv run python scripts/fetch_browser_test_assets.py")

    def handle(route: Any) -> None:
        local = root / route.request.url.split("/npm/", 1)[1].split("?", 1)[0]
        if not local.is_file():
            route.fulfill(status=404, body="not cached")
            return
        route.fulfill(
            body=local.read_bytes(),
            content_type=ASSET_TYPES.get(local.suffix, "application/octet-stream"),
        )

    context.route("https://cdn.jsdelivr.net/npm/**", handle)


@contextmanager
def mic_browser(
    playwright: Any, wav: Path | None = None, color_scheme: str = "light"
) -> Iterator[Any]:
    """A browser context with cached VAD assets; the fake microphone plays ``wav`` once.

    Without ``wav`` the microphone stays silent, which is enough for pages that never listen.
    """
    args = [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if wav is not None:
        args.append(f"--use-file-for-fake-audio-capture={wav}%noloop")
    browser = playwright.chromium.launch(headless=True, env=chromium_env(), args=args)
    context = browser.new_context(permissions=["microphone"], color_scheme=color_scheme)
    serve_vad_assets(context)
    try:
        yield context
    finally:
        context.close()
        browser.close()
