from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import requests
from pydub import AudioSegment

log = logging.getLogger(__name__)

# HTTP statuses worth retrying. 429 = rate-limit / model busy (DeepInfra's
# "Model busy, retry later"); 502/503/504 = transient backend hiccups.
RETRYABLE_STATUS = {429, 502, 503, 504}


class TranscriptionError(Exception):
    pass


class Transcriber:
    """Whisper-style transcription client.

    Supports two request formats:
      - "form-data": multipart/form-data (DeepInfra, standard Whisper API).
      - "json": JSON body with base64-encoded audio (OpenRouter audio/transcriptions).
    """

    def __init__(self, api_key: str, api_url: str, model: str, language: str,
                 request_format: str = "form-data",
                 request_timeout_sec: int = 120,
                 max_attempts: int = 3,
                 retry_backoff_sec: float = 2.0) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.language = language
        self.request_format = request_format  # "form-data" or "json"
        self.request_timeout_sec = request_timeout_sec
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff_sec = max(0.0, retry_backoff_sec)

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
            try:
                mp3_kb = mp3_path.stat().st_size // 1024
            except OSError:
                mp3_kb = -1
            # One line per attempt-batch tying payload size/duration to the
            # outcome that follows — makes 429/timeout failures diagnosable.
            log.info(
                "Transcribing %s: %dms audio, %dKB mp3, format=%s, model=%s",
                wav_path.name, len(audio), mp3_kb, self.request_format, self.model,
            )

            if self.request_format == "json":
                return self._post_json(
                    mp3_path, timeout_sec, wav_path.name, convert_ms
                )
            else:
                return self._post_form(mp3_path, timeout_sec, wav_path.name, convert_ms)
        finally:
            if mp3_path.exists():
                try:
                    mp3_path.unlink()
                except OSError:
                    pass

    # --- form-data (DeepInfra / standard Whisper API) ---

    def _post_form(self, mp3_path: Path, timeout_sec: int,
                   wav_name: str, convert_ms: int) -> str:
        """Multipart/form-data request (original DeepInfra-style)."""
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
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

                if response.status_code == 200:
                    result = response.json()
                    if "text" not in result:
                        log.error("API response missing 'text' field: %s", result)
                        raise TranscriptionError("API response missing 'text' field")
                    text = result["text"].strip()
                    log.info(
                        "Transcribed %s in %dms (convert %dms, api %dms, "
                        "attempt %d/%d): %d chars",
                        wav_name, convert_ms + api_ms, convert_ms, api_ms,
                        attempt, self.max_attempts, len(text),
                    )
                    return text

                if (response.status_code in RETRYABLE_STATUS
                        and attempt < self.max_attempts):
                    delay = self._compute_backoff(attempt, response)
                    log.warning(
                        "API %d on %s (attempt %d/%d), retrying in %.1fs: %s",
                        response.status_code, wav_name, attempt,
                        self.max_attempts, delay, response.text[:200],
                    )
                    time.sleep(delay)
                    continue

                log.error("Transcription API returned %d: %s",
                          response.status_code, response.text[:500])
                raise TranscriptionError(
                    f"API returned {response.status_code}: {response.text[:200]}"
                )

            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
                if attempt < self.max_attempts:
                    delay = self.retry_backoff_sec * (2 ** (attempt - 1))
                    log.warning(
                        "Network error on %s (attempt %d/%d), retrying "
                        "in %.1fs: %s",
                        wav_name, attempt, self.max_attempts, delay, e,
                    )
                    time.sleep(delay)
                    continue
                log.exception("HTTP error during transcription")
                raise TranscriptionError(str(e)) from e

        raise TranscriptionError(
            f"Exhausted {self.max_attempts} attempts; last error: {last_error}"
        )

    # --- json (OpenRouter audio/transcriptions) ---

    def _post_json(self, mp3_path: Path, timeout_sec: int,
                   wav_name: str, convert_ms: int) -> str:
        """JSON body request with base64-encoded audio (OpenRouter-style)."""
        last_error: Exception | None = None

        with mp3_path.open("rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload: dict = {
            "model": self.model,
            "input_audio": {
                "data": audio_b64,
                "format": "mp3",
            },
        }
        log.debug(
            "JSON request: model=%s, payload %d bytes (base64 %d bytes)",
            self.model, mp3_path.stat().st_size, len(audio_b64),
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(1, self.max_attempts + 1):
            try:
                t1 = time.monotonic()
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=timeout_sec,
                )
                api_ms = int((time.monotonic() - t1) * 1000)

                if response.status_code == 200:
                    result = response.json()
                    if "text" not in result:
                        log.error("API response missing 'text' field: %s", result)
                        raise TranscriptionError("API response missing 'text' field")
                    text = result["text"].strip()
                    log.info(
                        "Transcribed %s in %dms (convert %dms, api %dms, "
                        "attempt %d/%d): %d chars",
                        wav_name, convert_ms + api_ms, convert_ms, api_ms,
                        attempt, self.max_attempts, len(text),
                    )
                    return text

                if (response.status_code in RETRYABLE_STATUS
                        and attempt < self.max_attempts):
                    delay = self._compute_backoff(attempt, response)
                    log.warning(
                        "API %d on %s (attempt %d/%d), retrying in %.1fs: %s",
                        response.status_code, wav_name, attempt,
                        self.max_attempts, delay, response.text[:200],
                    )
                    time.sleep(delay)
                    continue

                log.error("Transcription API returned %d: %s",
                          response.status_code, response.text[:500])
                raise TranscriptionError(
                    f"API returned {response.status_code}: {response.text[:200]}"
                )

            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
                if attempt < self.max_attempts:
                    delay = self.retry_backoff_sec * (2 ** (attempt - 1))
                    log.warning(
                        "Network error on %s (attempt %d/%d), retrying "
                        "in %.1fs: %s",
                        wav_name, attempt, self.max_attempts, delay, e,
                    )
                    time.sleep(delay)
                    continue
                log.exception("HTTP error during transcription")
                raise TranscriptionError(str(e)) from e

        raise TranscriptionError(
            f"Exhausted {self.max_attempts} attempts; last error: {last_error}"
        )

    def _compute_backoff(self, attempt: int, response) -> float:
        # Servers may provide an explicit Retry-After (seconds) — honor it.
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                value = float(retry_after)
                if value > 0:
                    return min(value, 60.0)
            except (TypeError, ValueError):
                pass
        return self.retry_backoff_sec * (2 ** (attempt - 1))
