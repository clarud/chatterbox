"""Settings outside the repository; API keys protected by Windows user DPAPI."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re

from .windows import bind, kernel32


PROVIDER_NAMES = {"openai": "OpenAI", "openrouter": "OpenRouter"}
PROVIDER_URLS = {"openai": "https://api.openai.com/v1", "openrouter": "https://openrouter.ai/api/v1"}
MODEL_DEFAULTS = {"openai": ("gpt-transcribe", "gpt-5.6-terra"),
                  "openrouter": ("openai/gpt-transcribe", "openai/gpt-5.6-terra")}


@dataclass
class Settings:
    hotkey: str = "Ctrl+Alt+Space"
    transcription_model: str = "gpt-transcribe"
    refinement_model: str = "gpt-5.6-terra"
    provider: str = "openai"
    theme: str = "light"
    output_mode: str = "polished"
    overlay_position: str = "bottom-center"
    overlay_margin: int = 24
    transcription_backend: str = "cloud"
    refinement_backend: str = "cloud"
    local_model: str = "base"
    track_application: bool = True
    collect_window_title: bool = False

    @classmethod
    def for_provider(cls, provider, hotkey="Ctrl+Alt+Space"):
        if provider not in PROVIDER_NAMES:
            raise ValueError("Select OpenAI or OpenRouter.")
        speech, refinement = MODEL_DEFAULTS[provider]
        return cls(hotkey, speech, refinement, provider)

    def validate(self):
        from .windows import parse_hotkey
        parse_hotkey(self.hotkey)
        if self.provider not in PROVIDER_NAMES:
            raise ValueError("Select OpenAI or OpenRouter.")
        if self.theme not in ("light", "dark"):
            raise ValueError("Theme must be light or dark.")
        if self.output_mode not in ("raw", "polished"):
            raise ValueError("Output mode must be Raw or Polished.")
        if self.overlay_position not in ("bottom-center", "bottom-left", "bottom-right", "top-center"):
            raise ValueError("Select a supported overlay position.")
        if type(self.overlay_margin) is not int or not 8 <= self.overlay_margin <= 160:
            raise ValueError("Overlay margin must be an integer from 8 to 160 logical pixels.")
        if self.transcription_backend not in ("cloud", "local"):
            raise ValueError("Select Cloud API or Local Whisper transcription.")
        if self.refinement_backend not in ("cloud", "local", "local_llm"):
            raise ValueError("Select Cloud, Local (rule-based), or Local (LLM) refinement.")
        if self.refinement_backend != "cloud" and self.transcription_backend != "local":
            raise ValueError("Local refinement requires Local Whisper transcription. Set the transcription source to Local Whisper, or switch refinement back to Cloud.")
        if self.local_model not in ("tiny", "base", "small"):
            raise ValueError("Select the tiny, base, or small local model.")
        if type(self.track_application) is not bool or type(self.collect_window_title) is not bool:
            raise ValueError("Tracking preferences must be true or false.")
        pattern = r"[a-zA-Z0-9._-]{1,100}" if self.provider == "openai" else r"[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*(?::[a-zA-Z0-9._-]+)?"
        for model in (self.transcription_model, self.refinement_model):
            if len(model) > 200 or not re.fullmatch(pattern, model):
                raise ValueError("Use a model ID valid for the provider; OpenRouter needs a provider/model slug, e.g. openai/gpt-transcribe.")


class DATA_BLOB(C.Structure):
    _fields_ = [("cbData", W.DWORD), ("pbData", C.POINTER(C.c_ubyte))]


crypt32 = C.WinDLL("crypt32", use_last_error=True)
Protect = bind(crypt32, "CryptProtectData", W.BOOL, C.POINTER(DATA_BLOB), W.LPCWSTR,
               C.c_void_p, C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(DATA_BLOB))
Unprotect = bind(crypt32, "CryptUnprotectData", W.BOOL, C.POINTER(DATA_BLOB), C.c_void_p,
                 C.c_void_p, C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(DATA_BLOB))
LocalFree = bind(kernel32, "LocalFree", C.c_void_p, C.c_void_p)


def _crypt(data: bytes, decrypt=False):
    buffer = (C.c_ubyte * len(data)).from_buffer_copy(data)
    source = DATA_BLOB(len(data), buffer)
    output = DATA_BLOB()
    if decrypt:
        ok = Unprotect(C.byref(source), None, None, None, None, 1, C.byref(output))
    else:
        ok = Protect(C.byref(source), "Chatterbox API key", None, None, None, 1, C.byref(output))
    if not ok:
        raise RuntimeError("Windows could not unlock/store the API key. Enter it again on this Windows account.")
    try:
        return C.string_at(output.pbData, output.cbData)
    finally:
        C.memset(output.pbData, 0, output.cbData)
        LocalFree(output.pbData)
        C.memset(buffer, 0, len(buffer))


class Store:
    def __init__(self, directory=None):
        if directory:
            self.directory = Path(directory)
        else:
            self.directory = Path(os.environ["LOCALAPPDATA"]) / "Chatterbox"
            self._migrate_legacy_directory(Path(os.environ["LOCALAPPDATA"]) / "VoiceInput")
        self.config_path = self.directory / "settings.json"
        self.key_path = self.directory / "api-key.dpapi"  # Legacy OpenAI key remains OpenAI-only.

    def _migrate_legacy_directory(self, legacy):
        """One-time move from this app's previous name (Voice Input); carries
        settings/keys/history/transcripts/models over untouched. Safe to skip
        if already migrated, never installed under the old name, or the
        rename can't complete (different drive, a file still open, etc.) —
        the app then simply starts fresh rather than losing anything."""
        try:
            if legacy.is_dir() and not self.directory.exists():
                legacy.rename(self.directory)
        except OSError:
            pass

    def key_path_for(self, provider="openai"):
        if provider not in PROVIDER_NAMES:
            raise ValueError("Unknown API provider.")
        return self.key_path if provider == "openai" else self.directory / "openrouter-api-key.dpapi"

    def load(self):
        if not self.config_path.exists():
            return Settings.for_provider("openai" if self.key_path.exists() else "openrouter")
        try:
            values = json.loads(self.config_path.read_text(encoding="utf-8"))
            values.setdefault("provider", "openai")  # Preserve pre-provider settings without rewriting them.
            settings = Settings(**values)
            settings.validate()
            return settings
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("Settings are invalid. Rename settings.json in %LOCALAPPDATA%\\Chatterbox.") from exc

    def _write(self, path, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    def save(self, settings):
        settings.validate()
        self._write(self.config_path, json.dumps(asdict(settings), indent=2).encode("utf-8"))

    def save_key(self, key, provider="openai"):
        key = key.strip()
        if not key or len(key) > 2048 or any(char.isspace() for char in key) or not key.isascii():
            raise ValueError("Enter a valid API key, without spaces.")
        self._write(self.key_path_for(provider), _crypt(key.encode("utf-8")))

    def get_key(self, provider="openai"):
        path = self.key_path_for(provider)
        if not path.exists():
            raise RuntimeError(f"Set your {PROVIDER_NAMES[provider]} API key before recording.")
        return _crypt(path.read_bytes(), decrypt=True).decode("utf-8")

    def delete_key(self, provider="openai"):
        self.key_path_for(provider).unlink(missing_ok=True)
