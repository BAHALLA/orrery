"""Sync semantics: idempotence, stale pruning, and failure containment."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from orrery_core.knowledge.config import KnowledgeConfig
from orrery_core.knowledge.models import Document
from orrery_core.knowledge.sources import FilesystemSource, GitSource
from orrery_core.knowledge.sync import sync_sources

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _FakeIndex:
    """In-memory stand-in implementing the KnowledgeIndex protocol."""

    def __init__(self):
        self.chunks: dict[str, dict] = {}
        self.ready = False

    async def ensure_ready(self):
        self.ready = True

    async def upsert(self, chunks):
        for chunk in chunks:
            self.chunks[chunk.id] = {"uri": chunk.uri, "revision": chunk.revision}

    async def delete_by_uri(self, uri):
        doomed = [k for k, v in self.chunks.items() if v["uri"] == uri]
        for key in doomed:
            del self.chunks[key]
        return len(doomed)

    async def delete_stale(self, uri, revision):
        doomed = [
            k for k, v in self.chunks.items() if v["uri"] == uri and v["revision"] != revision
        ]
        for key in doomed:
            del self.chunks[key]
        return len(doomed)

    async def indexed_revisions(self):
        return {v["uri"]: v["revision"] for v in self.chunks.values()}


class _FakeSource:
    def __init__(self, name, documents, error=None):
        self.name = name
        self._documents = documents
        self._error = error

    async def watermark(self):
        return None

    async def documents(self, since=None):
        if self._error:
            raise self._error
        for document in self._documents:
            yield document


def _doc(uri, text="# T\n\nbody\n", revision="rev1"):
    return Document(uri=uri, title="T", text=text, revision=revision, updated_at=NOW)


@pytest.fixture
def config():
    return KnowledgeConfig(orrery_knowledge_max_chars=400, orrery_knowledge_overlap_chars=50)


@pytest.mark.asyncio
async def test_indexes_documents_and_prepares_the_backend(config):
    index = _FakeIndex()
    report = await sync_sources([_FakeSource("s", [_doc("file://a.md")])], index, config=config)
    assert index.ready
    assert report.indexed == 1
    assert report.chunks == len(index.chunks) == 1


@pytest.mark.asyncio
async def test_unchanged_documents_are_skipped_on_a_second_pass(config):
    index = _FakeIndex()
    source = _FakeSource("s", [_doc("file://a.md")])
    await sync_sources([source], index, config=config)
    report = await sync_sources([source], index, config=config)
    # Revision-driven skipping is the difference between re-indexing a corpus
    # and re-indexing the pages that actually moved.
    assert report.skipped == 1
    assert report.indexed == 0


@pytest.mark.asyncio
async def test_a_shrinking_document_leaves_no_orphan_chunks(config):
    index = _FakeIndex()
    long_doc = _doc(
        "file://a.md",
        text="# A\n\n" + "\n\n".join(f"para {i} " + "word " * 60 for i in range(6)),
        revision="rev1",
    )
    await sync_sources([_FakeSource("s", [long_doc])], index, config=config)
    assert len(index.chunks) > 1

    # The edit that would otherwise keep answering queries from slices that no
    # longer exist in the source.
    short_doc = _doc("file://a.md", text="# A\n\nnow tiny\n", revision="rev2")
    report = await sync_sources([_FakeSource("s", [short_doc])], index, config=config)
    assert report.pruned >= 1
    assert len(index.chunks) == 1
    assert all(v["revision"] == "rev2" for v in index.chunks.values())


@pytest.mark.asyncio
async def test_deleted_documents_are_removed_from_the_index(config):
    index = _FakeIndex()
    await sync_sources(
        [_FakeSource("s", [_doc("file://a.md"), _doc("file://b.md")])], index, config=config
    )
    report = await sync_sources([_FakeSource("s", [_doc("file://a.md")])], index, config=config)
    # A retired runbook must stop being retrievable.
    assert report.removed_documents == 1
    assert {v["uri"] for v in index.chunks.values()} == {"file://a.md"}


@pytest.mark.asyncio
async def test_a_failing_source_never_empties_the_index(config):
    index = _FakeIndex()
    await sync_sources([_FakeSource("s", [_doc("file://a.md")])], index, config=config)
    before = dict(index.chunks)

    # A source that failed halfway looks identical to one whose documents were
    # all deleted. Acting on that ambiguity would empty the corpus because a
    # Confluence token expired.
    report = await sync_sources(
        [_FakeSource("s", [], error=RuntimeError("token expired"))], index, config=config
    )
    assert report.errors
    assert report.removed_documents == 0
    assert index.chunks == before


@pytest.mark.asyncio
async def test_one_bad_document_does_not_abandon_the_rest(config):
    index = _FakeIndex()

    class _PartialIndex(_FakeIndex):
        async def upsert(self, chunks):
            if chunks[0].uri == "file://bad.md":
                raise RuntimeError("mapper_parsing_exception")
            await super().upsert(chunks)

    index = _PartialIndex()
    report = await sync_sources(
        [_FakeSource("s", [_doc("file://bad.md"), _doc("file://good.md")])],
        index,
        config=config,
    )
    assert report.indexed == 1
    assert len(report.errors) == 1
    assert any(v["uri"] == "file://good.md" for v in index.chunks.values())


@pytest.mark.asyncio
async def test_prune_can_be_disabled_for_partial_syncs(config):
    index = _FakeIndex()
    await sync_sources(
        [_FakeSource("s", [_doc("file://a.md"), _doc("file://b.md")])], index, config=config
    )
    report = await sync_sources(
        [_FakeSource("s", [_doc("file://a.md")])], index, config=config, prune_deleted=False
    )
    assert report.removed_documents == 0
    assert len({v["uri"] for v in index.chunks.values()}) == 2


# ── Sources ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filesystem_source_reads_titles_and_stable_uris(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "kafka.md").write_text("# Kafka ISR shrink\n\nbody\n")
    (tmp_path / "nested" / "k8s.md").write_text("body without heading\n")
    (tmp_path / "ignored.txt").write_text("not markdown")

    documents = [d async for d in FilesystemSource(tmp_path, name="runbooks").documents()]
    by_uri = {d.uri: d for d in documents}

    assert set(by_uri) == {"file://kafka.md", "file://nested/k8s.md"}
    assert by_uri["file://kafka.md"].title == "Kafka ISR shrink"
    # No H1 — fall back to a humanised filename rather than an empty citation.
    assert by_uri["file://nested/k8s.md"].title == "k8s"
    labels = by_uri["file://kafka.md"].labels
    assert labels["source"] == "runbooks"
    # `path` is what a citation resolves to on disk.
    assert labels["path"] == "kafka.md"


@pytest.mark.asyncio
async def test_filesystem_source_skips_empty_and_oversized(tmp_path, monkeypatch):
    (tmp_path / "empty.md").write_text("   \n\n")
    (tmp_path / "big.md").write_text("x" * 200)
    monkeypatch.setattr("orrery_core.knowledge.sources.filesystem.MAX_DOCUMENT_BYTES", 100)
    documents = [d async for d in FilesystemSource(tmp_path).documents()]
    assert documents == []


@pytest.mark.asyncio
async def test_missing_root_is_an_error_not_a_silent_empty_sync(tmp_path):
    with pytest.raises(FileNotFoundError):
        [d async for d in FilesystemSource(tmp_path / "nope").documents()]


@pytest.mark.asyncio
async def test_git_source_falls_back_for_untracked_files(tmp_path):
    # No repository here, so every file is "untracked" — it must still index,
    # just without claiming a commit as its revision.
    (tmp_path / "a.md").write_text("# A\n\nbody\n")
    documents = [d async for d in GitSource(tmp_path, name="adr").documents()]
    assert len(documents) == 1
    assert documents[0].uri == "git://a.md"
    assert documents[0].revision.startswith("untracked:")


def test_git_gets_its_env_config_but_not_the_process_secrets(tmp_path, monkeypatch):
    from orrery_core.knowledge.sources import filesystem

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("JWT_SECRET", "hunter2")
    with patch.object(filesystem.subprocess, "run") as run:
        run.return_value.stdout = ""
        filesystem._git(tmp_path, "log")
    env = run.call_args.kwargs["env"]
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert "JWT_SECRET" not in env
