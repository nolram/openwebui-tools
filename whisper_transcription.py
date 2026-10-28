"""
title: Whisper Transcription
author: Marlon (nolram)
description: Transcribe audio files (OGG, OPUS, MP3, WAV, M4A) via the Hades cluster Whisper service (faster-whisper).
required_open_webui_version: 0.4.0
version: 1.0.0
"""

import os

import requests
from pydantic import BaseModel, Field


AUDIO_EXTS = (".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".wma", ".webm", ".amr")


class Tools:
    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        whisper_base_url: str = Field(
            "http://whisper-transcription.aiassist-bot-core.svc.cluster.local:8000",
            description="Base URL of the Whisper transcription service (no trailing slash).",
        )
        timeout_seconds: int = Field(300, description="Transcription request timeout in seconds.")

    async def transcribe_audio(
        self,
        file_path: str = "",
        language: str = "",
        task: str = "transcribe",
        __files__: list = None,
    ) -> str:
        """Transcribe an audio file into text using the Whisper service.

        Use this whenever the user wants to transcribe, convert to text, or "read"
        an audio / voice message (OGG, OPUS, MP3, WAV, M4A, etc.).

        :param file_path: Absolute path (readable from the Open WebUI server) to the audio file.
            Leave empty to use the audio file attached to the current chat message.
        :param language: Optional ISO-639-1 code to force the language (e.g. 'pt', 'en').
            Empty = auto-detect.
        :param task: 'transcribe' to keep the original language, or 'translate' for English.
        :return: The transcribed text.
        """
        target = self._resolve_audio_file(file_path, __files__ or [])
        if target is None:
            return (
                "Error: no audio file found. Attach an audio file to the message, "
                "or pass file_path (absolute path readable by the Open WebUI server)."
            )

        data = {}
        if language:
            data["language"] = language
        if task not in ("", "transcribe"):
            data["task"] = task

        base = (self.valves.whisper_base_url or "").rstrip("/")
        try:
            with open(target, "rb") as fh:
                resp = requests.post(
                    f"{base}/transcribe",
                    files={"file": (os.path.basename(target), fh, "application/octet-stream")},
                    data=data,
                    timeout=self.valves.timeout_seconds,
                )
        except requests.RequestException as exc:
            return f"Error: could not reach the Whisper service: {exc}"

        if resp.status_code != 200:
            return f"Error: Whisper returned HTTP {resp.status_code}: {resp.text}"
        try:
            payload = resp.json()
        except ValueError:
            return f"Error: Whisper returned a non-JSON response: {resp.text}"

        text = (payload.get("text") or "").strip()
        if not text:
            return "Warning: Whisper succeeded but returned an empty transcript."
        return text

    async def whisper_health(self) -> str:
        """Check whether the Whisper transcription service is reachable and healthy."""
        base = (self.valves.whisper_base_url or "").rstrip("/")
        try:
            resp = requests.get(f"{base}/health", timeout=15)
        except requests.RequestException as exc:
            return f"Error: could not reach the Whisper service: {exc}"
        if resp.status_code == 200:
            return f"Whisper service healthy: {resp.text}"
        return f"Whisper returned HTTP {resp.status_code}: {resp.text}"

    def _resolve_audio_file(self, file_path: str, files: list):
        if file_path:
            if os.path.isabs(file_path) and os.path.exists(file_path):
                return file_path
            return None

        for entry in files or []:
            try:
                file_obj = entry.get("file") or entry
                path = file_obj.get("path")
                name = file_obj.get("name") or os.path.basename(path or "")
                looks_audio = (path or "").lower().endswith(AUDIO_EXTS) or name.lower().endswith(AUDIO_EXTS)
                if path and looks_audio and os.path.exists(path):
                    return path
            except AttributeError:
                continue
        return None
