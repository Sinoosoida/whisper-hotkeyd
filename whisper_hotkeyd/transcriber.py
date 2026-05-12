from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests
from pydub import AudioSegment

log = logging.getLogger(__name__)


class TranscriptionError(Exception):
    pass


class Transcriber:
    def __init__(self, api_key: str, api_url: str, model: str, language: str,
                 request_timeout_sec: int = 120) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.language = language
        self.request_timeout_sec = request_timeout_sec

    def transcribe(self, wav_path: Path, timeout_sec: int | None = None) -> str:
        if timeout_sec is None:
            timeout_sec = self.request_timeout_sec
        if not self.api_key:
            raise TranscriptionError("API key is not set in config")

        mp3_path = wav_path.with_suffix(".mp3")
        try:
            t0 = time.monotonic()
            log.debug("Converting %s -> %s", wav_path.name, mp3_path.name)
            audio = AudioSegment.from_wav(str(wav_path))
            audio.export(str(mp3_path), format="mp3", bitrate="64k")
            convert_ms = int((time.monotonic() - t0) * 1000)

            t1 = time.monotonic()
            with mp3_path.open("rb") as f:
                response = requests.post(
                    self.api_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (mp3_path.name, f, "audio/mpeg")},
                    data={"model": self.model, "language": self.language},
                    timeout=timeout_sec,
                )
            api_ms = int((time.monotonic() - t1) * 1000)

            if response.status_code != 200:
                log.error("Transcription API returned %d: %s",
                          response.status_code, response.text[:500])
                raise TranscriptionError(
                    f"API returned {response.status_code}: {response.text[:200]}"
                )

            result = response.json()
            if "text" not in result:
                log.error("API response missing 'text' field: %s", result)
                raise TranscriptionError("API response missing 'text' field")

            text = result["text"].strip()
            log.info("Transcribed %s in %dms (convert %dms, api %dms): %d chars",
                     wav_path.name, convert_ms + api_ms, convert_ms, api_ms, len(text))
            return text

        except requests.RequestException as e:
            log.exception("HTTP error during transcription")
            raise TranscriptionError(str(e)) from e
        finally:
            if mp3_path.exists():
                try:
                    mp3_path.unlink()
                except OSError:
                    pass
