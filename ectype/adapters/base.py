"""Adapter contract. One subclass per agent; nothing else in the package knows vendor formats."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from .. import settings
from ..model import Session, SessionRef

CATEGORIES = {"coding": "coding agent", "chat": "chat app"}


def app_config_dir(app: str, sub: str = "User") -> str:
    """Per-platform config dir of a VS Code-family app (Code, Cursor, VSCodium ...).
    Windows: %APPDATA%/<app>   macOS: ~/Library/Application Support/<app>   Linux: ~/.config/<app>"""
    if os.name == "nt":
        return f"%APPDATA%/{app}/{sub}"
    if sys.platform == "darwin":
        return f"~/Library/Application Support/{app}/{sub}"
    return f"~/.config/{app}/{sub}"


def scratch_dir(prefix: str) -> Path:
    """A temporary directory removed when the process exits, for the single-session database an
    `artifacts()` export builds out of a SQLite store. It used to be a bare `mkdtemp` that nothing
    ever deleted: one directory per fixture run, for good."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d


class Adapter(ABC):
    name: str                 # machine id, e.g. "claude-code"
    label: str                # human label, e.g. "Claude Code"
    env_home: str             # env var that overrides the store location (wins over settings)
    default_home: str         # default store, relative to ~ (may contain ~ and %VARS%)
    category: str = "coding"  # "coding" agents are shown by default; "chat" apps are opt-in in Settings
    writable: bool = False    # a converter can write this agent's format so it can be RESUMED
    native_format: str | None = None  # human name of that resumable format, e.g. "Claude Code .jsonl"

    def _home_raw(self) -> tuple[str, str]:
        """(path as written, where it came from): env var > settings file > adapter default."""
        env = os.environ.get(self.env_home)
        if env:
            return env, "env"
        saved = settings.agent_home(self.name)
        if saved:
            return saved, "settings"
        return self.default_home, "default"

    def home(self) -> Path:
        raw, _ = self._home_raw()
        return Path(os.path.expanduser(os.path.expandvars(raw)))

    def home_source(self) -> str:
        """'env' | 'settings' | 'default': which layer decided home()."""
        return self._home_raw()[1]

    def available(self) -> bool:
        return self.home().exists()

    def enabled(self) -> bool:
        """Listed in the GUI? Settings decide; default = coding agents yes, chat apps no."""
        return settings.agent_enabled(self.name, default=self.category == "coding")

    @abstractmethod
    def discover(self) -> list[SessionRef]:
        """Cheap enumeration; never parses whole transcripts."""

    @abstractmethod
    def load(self, ref: SessionRef) -> Session:
        """Full parse into the canonical model."""

    def artifacts(self, ref: SessionRef) -> list[tuple[Path, str]]:
        """Every file that makes up this session: (absolute source, relative destination)."""
        return [(ref.path, ref.path.name)]

    def rename(self, ref: SessionRef, title: str) -> bool:
        """Write `title` into the agent's OWN store, and say whether that happened.

        False by default, and that is the honest answer for most agents: their stores have no
        place for a name a human chose, and inventing one would mean writing into another
        program's database on a guess. `ectype.titles` keeps the name locally instead. Override
        only where the store has a real field for it (see the Claude Code adapter, which writes
        the same `custom-title.json` the agent writes itself). An empty `title` clears it."""
        return False

    def workspaces(self) -> list[str]:
        """Distinct working directories this agent has sessions for: the places an installed
        conversion can land. Empty when the agent records none."""
        return []

    def find(self, id_prefix: str) -> list[SessionRef]:
        return [r for r in self.discover() if r.id.startswith(id_prefix)]
