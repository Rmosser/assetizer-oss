"""Runtime configuration.

Everything is driven by environment variables so the same code runs on the
OVH worker and on a developer laptop. ``ASSETIZER_HOME`` is the only path that
normally needs to be set; every other location derives from it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def secret_env(name: str) -> str:
    """The environment variable that takes precedence over ``secrets/<name>`` (e.g. ASSETIZER_COOKIECLOUD_TOKEN)."""
    return "ASSETIZER_" + name.upper().replace("-", "_")


@dataclass(frozen=True)
class Settings:
    home: Path
    agy_bin: str
    agy_model: str
    agy_state_dir: Path
    ytdlp_bin: str
    deno_bin: str | None
    browser_profile: Path | None
    whisper_model: str
    whisper_root: Path
    font_dir: Path | None
    web_host: str
    web_port: int
    # Media / scheduling policy
    chunk_minutes: int = 50
    chunk_overlap_minutes: int = 5
    single_chunk_max_minutes: int = 55
    boundary_pad_seconds: int = 30
    disk_high_watermark: float = 0.85
    source_retention_days: int = 7
    log_retention_days: int = 30  # the logs iterating reads (logs.py)
    agy_print_timeout: str = "35m"
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.home / "assetizer.db"

    @property
    def media_dir(self) -> Path:
        return self.home / "media"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def packages_dir(self) -> Path:
        return self.home / "packages"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"

    def secret(self, name: str) -> str | None:
        env = os.environ.get(secret_env(name))
        if env:
            return env.strip()
        p = self.secrets_dir / name
        if p.exists():
            return p.read_text().strip() or None
        return None

    def ensure_dirs(self) -> None:
        for d in (self.home, self.media_dir, self.runs_dir, self.packages_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.secrets_dir.chmod(0o700)
        except OSError:
            pass


def load() -> Settings:
    home = Path(_env("ASSETIZER_HOME", str(Path.cwd() / "var"))).expanduser()
    profile = _env("ASSETIZER_BROWSER_PROFILE", "")
    font = _env("ASSETIZER_FONT_DIR", "")
    deno = _env("ASSETIZER_DENO", "")
    return Settings(
        home=home,
        agy_bin=_env("ASSETIZER_AGY", "agy"),
        agy_model=_env("ASSETIZER_MODEL", "gemini-3.8-flash-high"),
        agy_state_dir=Path(_env("ASSETIZER_AGY_STATE", str(Path.home() / ".gemini/antigravity-cli"))).expanduser(),
        ytdlp_bin=_env("ASSETIZER_YTDLP", "yt-dlp"),
        deno_bin=deno or None,
        browser_profile=Path(profile).expanduser() if profile else None,
        whisper_model=_env("ASSETIZER_WHISPER_MODEL", "large-v3-turbo"),
        whisper_root=Path(_env("ASSETIZER_WHISPER_ROOT", str(home / "models"))).expanduser(),
        font_dir=Path(font).expanduser() if font else None,
        web_host=_env("ASSETIZER_HOST", "127.0.0.1"),
        web_port=int(_env("ASSETIZER_PORT", "8770")),
        source_retention_days=int(_env("ASSETIZER_RETENTION_DAYS", "7")),
        log_retention_days=int(_env("ASSETIZER_LOG_RETENTION_DAYS", "30")),
    )


_settings: Settings | None = None


def get() -> Settings:
    global _settings
    if _settings is None:
        _settings = load()
    return _settings


def override(s: Settings) -> None:
    """Test hook."""
    global _settings
    _settings = s
