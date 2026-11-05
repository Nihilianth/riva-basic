"""The page scripts are Python strings; a syntax slip would only show up in a browser."""

import shutil
import subprocess
from pathlib import Path

import pytest

from riva.experiments.asr_stream import VAD_BROWSER_JS
from riva.experiments.voice_reply import PLAYBACK_JS

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.mark.parametrize(
    "name,script", [("vad", VAD_BROWSER_JS), ("playback", PLAYBACK_JS)], ids=["vad", "playback"]
)
def test_embedded_script_has_valid_javascript_syntax(
    tmp_path: Path, name: str, script: str
) -> None:
    path = tmp_path / f"{name}.js"
    path.write_text(f"const f = {script.strip()};\n" if name == "vad" else script)
    result = subprocess.run(
        ["node", "--check", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
