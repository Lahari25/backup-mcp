"""Backup-ops MCP server: snapshot, diff, verify and restore local directories."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from mcp.server.elicitation import ElicitationResult
from mcp.server.mcpserver import AcceptedElicitation, Elicit, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from backup_mcp.store import (
    DiffResult,
    RestoreResult,
    SnapshotError,
    SnapshotInfo,
    SnapshotStore,
    VerifyResult,
    load_manifest_json,
)


class RestoreConfirmation(BaseModel):
    confirm: bool = Field(description="Restore these files now?")


def ask_to_confirm_restore(
    snapshot_id: str, target: str, files: list[str] | None = None
) -> Elicit[RestoreConfirmation]:
    """Resolver: ask the user before a restore writes anything."""
    count = "all files" if files is None else f"{len(files)} file(s)"
    return Elicit(f"Restore {count} from snapshot {snapshot_id} into {target}?", RestoreConfirmation)


def build_server(store_root: str | os.PathLike[str] | None = None) -> MCPServer:
    store = SnapshotStore(Path(store_root or os.environ.get("BACKUP_MCP_ROOT", "~/.backup-mcp")))
    mcp = MCPServer(
        "backup-ops",
        instructions=(
            "Snapshot local directories and restore them safely. Restores never overwrite: "
            "they go to an empty or new target directory. Run verify_snapshot before a restore "
            "if a snapshot is old."
        ),
    )

    def _guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SnapshotError as error:
            raise ToolError(str(error)) from error

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    def create_snapshot(source: str, label: str | None = None) -> SnapshotInfo:
        """Copy every regular file under `source` into a new checksummed snapshot."""
        return _guard(store.create, source, label)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_snapshots(source: str | None = None) -> list[SnapshotInfo]:
        """List snapshots, newest first, optionally only those taken of `source`."""
        return _guard(store.list, source)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def diff_snapshot(snapshot_id: str) -> DiffResult:
        """Compare a snapshot with the current contents of its source directory."""
        return _guard(store.diff, snapshot_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def verify_snapshot(snapshot_id: str) -> VerifyResult:
        """Re-hash a snapshot's stored files and report missing or corrupted ones."""
        return _guard(store.verify, snapshot_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    def restore_snapshot(
        snapshot_id: str,
        target: str,
        confirmation: Annotated[ElicitationResult[RestoreConfirmation], Resolve(ask_to_confirm_restore)],
        files: list[str] | None = None,
    ) -> RestoreResult:
        """Restore a snapshot (or selected relative paths) into an empty or new `target` directory.

        The user is asked to confirm before anything is written.
        """
        if not (isinstance(confirmation, AcceptedElicitation) and confirmation.data.confirm):
            raise ToolError("restore not confirmed; nothing was written")
        return _guard(store.restore, snapshot_id, target, files)

    @mcp.resource("snapshot://{snapshot_id}/manifest", mime_type="application/json")
    def snapshot_manifest(snapshot_id: str) -> str:
        """The full manifest of one snapshot, with per-file sizes and SHA-256 checksums."""
        return load_manifest_json(store, snapshot_id)

    @mcp.prompt()
    def plan_restore(snapshot_id: str, incident: str) -> str:
        """Walk through a safe restore for an incident."""
        return (
            f"An incident needs data from snapshot {snapshot_id}: {incident}\n"
            "1. Call verify_snapshot and stop if it is not ok.\n"
            "2. Call diff_snapshot to see what changed since the snapshot.\n"
            "3. Propose the smallest set of files to restore and an empty target directory.\n"
            "4. Ask for confirmation, then call restore_snapshot with those files."
        )

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
