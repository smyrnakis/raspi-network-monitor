"""Validated runtime settings stored separately from deployment configuration."""

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from .config import (
    AppConfig,
    ConfigError,
    apply_editable_settings,
    editable_settings,
)


class RuntimeSettingsStore:
    def __init__(self, base_config: AppConfig) -> None:
        self._base = base_config
        directory = base_config.storage.database_path.parent / "settings"
        self.path = directory / "runtime-settings.json"
        self.restart_request_path = directory / "settings-restart-request"
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            stored = self._read_stored()
            config = apply_editable_settings(self._base, stored)
            payload = editable_settings(config)
            payload["revision"] = stored.get("revision", 0)
            return payload

    def load_config(self) -> AppConfig:
        with self._lock:
            return apply_editable_settings(self._base, self._read_stored())

    def save(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._read_stored()
            config = apply_editable_settings(self._base, raw)
            payload = editable_settings(config)
            payload["revision"] = int(current.get("revision", 0)) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    json.dump(payload, stream, ensure_ascii=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o640)
                os.replace(temporary, self.path)
                self.restart_request_path.touch(mode=0o640, exist_ok=True)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return payload

    def consume_restart_request(self) -> bool:
        try:
            self.restart_request_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _read_stored(self) -> dict[str, Any]:
        if not self.path.exists():
            payload = editable_settings(self._base)
            payload["revision"] = 0
            return payload
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"cannot read runtime settings: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigError("runtime settings must contain a JSON object")
        revision = raw.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ConfigError("runtime settings revision must be a non-negative integer")
        return raw
