"""Cached voice previews: the same short German sentence in every voice, generated once.

A preview is one real TTS request (a few cents for all voices together), so results are kept on disk
under the private folder. The cache key covers model, voice and sentence, so changing any of them
makes a new file instead of serving an outdated one. Nothing here runs at import or start-up; a
preview is only generated when a person picks the voice (or the presenter confirms "prepare all").
"""

import hashlib
import os
import sys
import tempfile
import threading
from pathlib import Path

from riva.audio.recordings import private_directory
from riva.audio.replies import ReplyConfig, pcm_wav
from riva.demo.config import DemoConfig
from riva.demo.providers import SpeechSynth
from riva.event_log import Event, EventName, write_event


class VoiceError(Exception):
    """A preview could not be produced; no provider text is kept."""


class VoicePreviews:
    def __init__(self, directory: Path, synth: SpeechSynth, config: DemoConfig) -> None:
        self.directory = private_directory(directory)
        self.synth, self.config = synth, config
        self.generated = 0  # real synthesis requests made (for tests and the presenter)
        self._locks = {voice: threading.Lock() for voice in config.voices}

    def _key(self, voice: str) -> str:
        text = f"{self.config.tts_model}|{voice}|{self.config.preview_sentence}"
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    def path(self, voice: str) -> Path:
        if voice not in self.config.voices:
            raise ValueError("voice is not allowed")
        return self.directory / f"{voice}-{self._key(voice)}.wav"

    def cached(self, voice: str) -> bool:
        return self.path(voice).is_file()

    def cached_count(self) -> int:
        return sum(self.cached(voice) for voice in self.config.voices)

    def get(self, voice: str) -> Path:
        """The preview file, generated first if needed (one request even for concurrent callers)."""
        target = self.path(voice)
        with self._locks[voice]:
            if target.is_file():
                return target
            # Content-free: a preview is a real TTS request, so it is counted and names its voice.
            write_event(Event(EventName.TTS_REQUESTED, "preview", detail=voice), sys.stdout)
            try:
                reply_config = ReplyConfig(tts_model=self.config.tts_model, voice=voice)
                pcm = b"".join(self.synth.speech(self.config.preview_sentence, reply_config))
                wav = pcm_wav(pcm)
            except Exception as error:
                raise VoiceError from error
            fd, name = tempfile.mkstemp(prefix=".writing-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(wav)
                Path(name).replace(target)
            finally:
                Path(name).unlink(missing_ok=True)
            self.generated += 1
            return target

    def prepare_all(self, *, confirmed: bool) -> tuple[int, int]:
        """Generate every missing preview. Paid; refuses without confirmation."""
        if not confirmed:
            raise PermissionError("Preparing all voices needs explicit confirmation")
        before = self.generated
        had = self.cached_count()
        for voice in self.config.voices:
            self.get(voice)
        return self.generated - before, had
