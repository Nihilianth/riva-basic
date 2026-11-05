"""One-time download of the browser VAD assets so Playwright tests run without the internet.

The page loads these from jsDelivr (pinned versions, see VAD_BROWSER_JS). Tests serve the cached
copies through Playwright routing. Nothing here is paid; it only downloads public files into
~/.cache/riva-browser-test-assets (override with RIVA_BROWSER_ASSETS).

    uv run python scripts/fetch_browser_test_assets.py
"""

import os
import urllib.request
from pathlib import Path

BASE = "https://cdn.jsdelivr.net/npm/"
FILES = [
    "onnxruntime-web@1.14.0/dist/ort.js",
    "onnxruntime-web@1.14.0/dist/ort-wasm.wasm",
    "onnxruntime-web@1.14.0/dist/ort-wasm-simd.wasm",
    "onnxruntime-web@1.14.0/dist/ort-wasm-threaded.wasm",
    "onnxruntime-web@1.14.0/dist/ort-wasm-simd-threaded.wasm",
    "@ricky0123/vad-web@0.0.27/dist/bundle.min.js",
    "@ricky0123/vad-web@0.0.27/dist/vad.worklet.bundle.min.js",
    "@ricky0123/vad-web@0.0.27/dist/silero_vad_v5.onnx",
    "axe-core@4.10.2/axe.min.js",  # automated accessibility checks (tests only)
]


def cache_dir() -> Path:
    return Path(
        os.environ.get("RIVA_BROWSER_ASSETS", "~/.cache/riva-browser-test-assets")
    ).expanduser()


def main() -> None:
    root = cache_dir()
    for name in FILES:
        target = root / name
        if target.exists():
            print(f"have {name}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(BASE + name, timeout=60) as response:
            target.write_bytes(response.read())
        print(f"fetched {name} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
