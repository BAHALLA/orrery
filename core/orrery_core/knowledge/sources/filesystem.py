"""Filesystem and git knowledge sources.

Both walk a directory of markdown; they differ only in what counts as a
*revision*, and that difference matters more than it looks:

- :class:`FilesystemSource` uses ``mtime:size``, which is cheap and correct on
  a working copy but changes on a fresh clone or checkout even when content
  did not. Fine for a laptop, wasteful in CI.
- :class:`GitSource` uses the commit sha that last touched each file, which is
  content-derived and stable across clones — so CI re-indexes only what a merge
  actually changed, and a citation can name the exact revision an operator can
  check out.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from ...security.secrets import subprocess_env
from ..models import Document

logger = logging.getLogger("orrery.knowledge.sources")

#: Extensions treated as indexable documents.
DEFAULT_PATTERNS = ("*.md", "*.markdown")

#: Refuse to load a single file larger than this. A multi-megabyte generated
#: file (a vendored changelog, an exported log) chunks into hundreds of
#: near-useless passages and crowds real documentation out of every result set.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


def _title_from(path: Path, text: str) -> str:
    """First H1 if the document has one, else a humanised filename."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped:
            break
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.name


def _iter_files(root: Path, patterns: Iterable[str]) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        found.update(p for p in root.rglob(pattern) if p.is_file())
    return sorted(found)


class FilesystemSource:
    """Indexes a directory tree of markdown files."""

    def __init__(
        self,
        root: str | Path,
        *,
        name: str = "filesystem",
        patterns: Iterable[str] = DEFAULT_PATTERNS,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.name = name
        self._root = Path(root).resolve()
        self._patterns = tuple(patterns)
        self._labels = dict(labels or {})

    async def watermark(self) -> str | None:
        """No incremental support — mtimes are not an ordering across files."""
        return None

    async def documents(self, since: str | None = None) -> AsyncIterator[Document]:
        if not self._root.is_dir():
            raise FileNotFoundError(f"Knowledge source root does not exist: {self._root}")
        for path in await asyncio.to_thread(_iter_files, self._root, self._patterns):
            document = await asyncio.to_thread(self._load, path)
            if document is not None:
                yield document

    def _load(self, path: Path) -> Document | None:
        stat = path.stat()
        if stat.st_size > MAX_DOCUMENT_BYTES:
            logger.warning(
                "skipping oversized knowledge document",
                extra={"path": str(path), "bytes": stat.st_size},
            )
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return None
        relative = path.relative_to(self._root).as_posix()
        return Document(
            uri=f"file://{relative}",
            title=_title_from(path, text),
            text=text,
            revision=f"{int(stat.st_mtime)}:{stat.st_size}",
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            labels={**self._labels, "source": self.name, "path": relative},
        )


#: Where git looks for a repository, beyond its configuration (``GIT_CONFIG*``).
_GIT_ENV = ("GIT_CEILING_DIRECTORIES",)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 — fixed executable, no shell
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        # All of GIT_CONFIG*, numbered pairs included: without a safe.directory
        # set that way, git refuses the repository and we'd only see "untracked".
        env=subprocess_env(*_GIT_ENV, *(n for n in os.environ if n.startswith("GIT_CONFIG"))),
    )
    return result.stdout.strip()


class GitSource:
    """Indexes markdown tracked in a git repository.

    Revisions are the commit sha that last touched each file, so a document is
    re-indexed exactly when git says it changed.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        name: str = "git",
        subdir: str = ".",
        patterns: Iterable[str] = DEFAULT_PATTERNS,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.name = name
        self._root = Path(root).resolve()
        self._subdir = subdir
        self._patterns = tuple(patterns)
        self._labels = dict(labels or {})

    async def watermark(self) -> str | None:
        """The repository HEAD, recorded so a later sync can diff against it."""
        try:
            return await asyncio.to_thread(_git, self._root, "rev-parse", "HEAD")
        except subprocess.CalledProcessError, FileNotFoundError:
            return None

    async def documents(self, since: str | None = None) -> AsyncIterator[Document]:
        base = (self._root / self._subdir).resolve()
        if not base.is_dir():
            raise FileNotFoundError(f"Knowledge source root does not exist: {base}")
        for path in await asyncio.to_thread(_iter_files, base, self._patterns):
            document = await asyncio.to_thread(self._load, path)
            if document is not None:
                yield document

    def _load(self, path: Path) -> Document | None:
        stat = path.stat()
        if stat.st_size > MAX_DOCUMENT_BYTES:
            logger.warning(
                "skipping oversized knowledge document",
                extra={"path": str(path), "bytes": stat.st_size},
            )
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return None
        relative = path.relative_to(self._root).as_posix()
        sha, updated_at = self._revision(relative, stat.st_mtime)
        return Document(
            uri=f"git://{relative}",
            title=_title_from(path, text),
            text=text,
            revision=sha,
            updated_at=updated_at,
            labels={**self._labels, "source": self.name, "path": relative},
        )

    def _revision(self, relative: str, fallback_mtime: float) -> tuple[str, datetime]:
        """Last commit sha and date for one path.

        Falls back to filesystem metadata for a file that is untracked or newly
        added — an uncommitted runbook is still worth indexing locally, it just
        cannot claim a commit as its revision.
        """
        try:
            out = _git(self._root, "log", "-1", "--format=%H%x1f%cI", "--", relative)
        except subprocess.CalledProcessError, FileNotFoundError:
            out = ""
        if out and "\x1f" in out:
            sha, iso = out.split("\x1f", 1)
            try:
                return sha, datetime.fromisoformat(iso)
            except ValueError:
                pass
        return (
            f"untracked:{int(fallback_mtime)}",
            datetime.fromtimestamp(fallback_mtime, tz=UTC),
        )
