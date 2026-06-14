"""
Configuration loader: reads profile.yaml and merges with defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS: dict[str, Any] = {
    "headless": True,
    "delay_between_entries": 3,
    "max_entries_per_run": 50,
    "concurrency": 3,
    "skip_captcha": True,
    "log_file": "entries.log",
    "database": "sweepstakes.db",
    "captcha_service": "none",
    "captcha_api_key": "",
}


# ── Config class ─────────────────────────────────────────────────────────────

class Config:
    """Holds the user profile and runtime settings loaded from a YAML file."""

    def __init__(self, profile_path: str | Path = "profile.yaml") -> None:
        self.profile_path = Path(profile_path)
        self._data: dict[str, Any] = {}
        self.profile: dict[str, str] = {}
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._load()

    # ── Loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Parse the YAML file and populate profile + settings."""
        if not self.profile_path.exists():
            raise FileNotFoundError(
                f"Profile file not found: {self.profile_path}\n"
                "Copy profile.example.yaml to profile.yaml and fill in your details."
            )

        with self.profile_path.open("r", encoding="utf-8") as fh:
            self._data = yaml.safe_load(fh) or {}

        raw_profile = self._data.get("profile", {})
        if not raw_profile:
            raise ValueError(
                f"'profile' section missing or empty in {self.profile_path}"
            )
        self.profile = {k: str(v) for k, v in raw_profile.items()}

        raw_settings = self._data.get("settings", {})
        self.settings.update(raw_settings)

    # ── Convenience properties ────────────────────────────────────────────

    @property
    def headless(self) -> bool:
        return bool(self.settings.get("headless", True))

    @property
    def delay_between_entries(self) -> int | float:
        return self.settings.get("delay_between_entries", 3)

    @property
    def max_entries_per_run(self) -> int:
        return int(self.settings.get("max_entries_per_run", 50))

    @property
    def concurrency(self) -> int:
        return int(self.settings.get("concurrency", 3))

    @property
    def skip_captcha(self) -> bool:
        return bool(self.settings.get("skip_captcha", True))

    @property
    def log_file(self) -> str:
        return str(self.settings.get("log_file", "entries.log"))

    @property
    def database(self) -> str:
        return str(self.settings.get("database", "sweepstakes.db"))

    @property
    def captcha_service(self) -> str:
        return str(self.settings.get("captcha_service", "none"))

    @property
    def captcha_api_key(self) -> str:
        return str(self.settings.get("captcha_api_key", ""))

    # ── Display ───────────────────────────────────────────────────────────

    def display(self) -> dict[str, Any]:
        """Return a safe dict for display (email redacted)."""
        safe_profile = dict(self.profile)
        if "email" in safe_profile:
            safe_profile["email"] = _redact_email(safe_profile["email"])
        if "email_confirm" in safe_profile:
            safe_profile["email_confirm"] = _redact_email(safe_profile["email_confirm"])
        return {
            "profile": safe_profile,
            "settings": self.settings,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _redact_email(email: str) -> str:
    """Replace the local part of an email with asterisks for display."""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[:2]}***@{domain}"


def load_config(profile_path: str | Path = "profile.yaml") -> Config:
    """Convenience factory."""
    return Config(profile_path)
