"""Snapshot storage: content copies plus a JSON manifest with SHA-256 checksums."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

MANIFEST = "manifest.json"
FILES_DIR = "files"


class FileEntry(BaseModel):
    size: int
    sha256: str


class SnapshotInfo(BaseModel):
    snapshot_id: str
    source: str
    label: str | None = None
    created_at: datetime
    file_count: int
    total_bytes: int


class Manifest(SnapshotInfo):
    files: dict[str, FileEntry] = Field(default_factory=dict)


class DiffResult(BaseModel):
    snapshot_id: str
    added: list[str]
    removed: list[str]
    modified: list[str]
    unchanged_count: int


class VerifyResult(BaseModel):
    snapshot_id: str
    ok: bool
    missing: list[str]
    corrupted: list[str]


class RestoreResult(BaseModel):
    snapshot_id: str
    target: str
    restored: list[str]


class SnapshotError(ValueError):
    """A request the store refuses; reported to the client as a tool error."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan(source: Path) -> dict[str, FileEntry]:
    """Regular files under `source`, keyed by POSIX relative path. Symlinks are skipped."""
    entries: dict[str, FileEntry] = {}
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            entries[rel] = FileEntry(size=path.stat().st_size, sha256=_sha256(path))
    return entries


def _safe_relpath(rel: str) -> PurePosixPath:
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise SnapshotError(f"invalid file path in request: {rel!r}")
    return pure


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, snapshot_id: str) -> Path:
        if not snapshot_id or "/" in snapshot_id or snapshot_id in {".", ".."}:
            raise SnapshotError(f"invalid snapshot id: {snapshot_id!r}")
        path = self.root / snapshot_id
        if not (path / MANIFEST).is_file():
            raise SnapshotError(f"snapshot not found: {snapshot_id}")
        return path

    def manifest(self, snapshot_id: str) -> Manifest:
        return Manifest.model_validate_json((self._dir(snapshot_id) / MANIFEST).read_text())

    def create(self, source: str, label: str | None = None) -> SnapshotInfo:
        src = Path(source).expanduser().resolve()
        if not src.is_dir():
            raise SnapshotError(f"source is not a directory: {source}")
        if src == self.root or self.root in src.parents or src in self.root.parents:
            raise SnapshotError("source must not contain or be inside the snapshot store")
        files = _scan(src)
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        staging = self.root / f".{snapshot_id}.partial"
        for rel in files:
            dest = staging / FILES_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dest)
        manifest = Manifest(
            snapshot_id=snapshot_id,
            source=str(src),
            label=label,
            created_at=datetime.now(timezone.utc),
            file_count=len(files),
            total_bytes=sum(entry.size for entry in files.values()),
            files=files,
        )
        staging.mkdir(parents=True, exist_ok=True)
        (staging / MANIFEST).write_text(manifest.model_dump_json(indent=2))
        staging.rename(self.root / snapshot_id)  # publish atomically: no half-written snapshots
        return SnapshotInfo(**manifest.model_dump(exclude={"files"}))

    def list(self, source: str | None = None) -> list[SnapshotInfo]:
        wanted = str(Path(source).expanduser().resolve()) if source else None
        out = []
        for child in sorted(self.root.iterdir()):
            if child.name.startswith(".") or not (child / MANIFEST).is_file():
                continue
            m = Manifest.model_validate_json((child / MANIFEST).read_text())
            if wanted is None or m.source == wanted:
                out.append(SnapshotInfo(**m.model_dump(exclude={"files"})))
        return sorted(out, key=lambda s: s.created_at, reverse=True)

    def diff(self, snapshot_id: str) -> DiffResult:
        m = self.manifest(snapshot_id)
        src = Path(m.source)
        current = _scan(src) if src.is_dir() else {}
        added = sorted(set(current) - set(m.files))
        removed = sorted(set(m.files) - set(current))
        common = set(current) & set(m.files)
        modified = sorted(p for p in common if current[p].sha256 != m.files[p].sha256)
        return DiffResult(
            snapshot_id=snapshot_id,
            added=added,
            removed=removed,
            modified=modified,
            unchanged_count=len(common) - len(modified),
        )

    def verify(self, snapshot_id: str) -> VerifyResult:
        m = self.manifest(snapshot_id)
        base = self._dir(snapshot_id) / FILES_DIR
        missing, corrupted = [], []
        for rel, entry in m.files.items():
            path = base / rel
            if not path.is_file():
                missing.append(rel)
            elif _sha256(path) != entry.sha256:
                corrupted.append(rel)
        return VerifyResult(
            snapshot_id=snapshot_id, ok=not (missing or corrupted), missing=missing, corrupted=corrupted
        )

    def restore(self, snapshot_id: str, target: str, files: list[str] | None = None) -> RestoreResult:
        m = self.manifest(snapshot_id)
        dest_root = Path(target).expanduser().resolve()
        if dest_root.exists() and any(dest_root.iterdir()):
            raise SnapshotError(f"restore target must be empty or not exist: {target}")
        if dest_root == Path(m.source) or Path(m.source) in dest_root.parents:
            raise SnapshotError("restore target must be outside the snapshot's source directory")
        selected = list(m.files) if files is None else [str(_safe_relpath(f)) for f in files]
        unknown = [f for f in selected if f not in m.files]
        if unknown:
            raise SnapshotError(f"not in snapshot: {', '.join(unknown)}")
        verify = self.verify(snapshot_id)
        bad = set(verify.missing) | set(verify.corrupted)
        if bad & set(selected):
            raise SnapshotError(f"snapshot failed verification for: {', '.join(sorted(bad & set(selected)))}")
        base = self._dir(snapshot_id) / FILES_DIR
        for rel in selected:
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / rel, dest)
        return RestoreResult(snapshot_id=snapshot_id, target=str(dest_root), restored=sorted(selected))


def load_manifest_json(store: SnapshotStore, snapshot_id: str) -> str:
    return json.dumps(store.manifest(snapshot_id).model_dump(mode="json"), indent=2)
