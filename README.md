# backup-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant snapshot local directories and restore them safely.

Built on the MCP Python SDK 2.x (`MCPServer`), using structured tool output, tool annotations, a resource template, a prompt, and resolver-based elicitation for restore confirmation.

## Tools

| Tool | What it does | Annotations |
|---|---|---|
| `create_snapshot(source, label?)` | Copies every regular file under `source` into a new snapshot with SHA-256 checksums. Snapshots are staged and published atomically. | not read-only |
| `list_snapshots(source?)` | Lists snapshots, newest first. | read-only |
| `diff_snapshot(snapshot_id)` | Added / removed / modified files compared to the source today. | read-only |
| `verify_snapshot(snapshot_id)` | Re-hashes stored files; reports missing or corrupted ones. | read-only |
| `restore_snapshot(snapshot_id, target, files?)` | Restores all or selected files into an empty or new directory, after the user confirms. | not destructive |

Also: resource `snapshot://{snapshot_id}/manifest` (full manifest JSON) and prompt `plan_restore(snapshot_id, incident)`.

## Safety rules

- Restores never overwrite: the target must be empty or not exist, and must be outside the snapshot's source.
- Every restore asks the user to confirm (MCP elicitation). Declining writes nothing.
- A restore refuses files that fail checksum verification.
- Requested paths with `..` or absolute paths are rejected; symlinks are not followed when snapshotting.
- The snapshot store can't be inside the directory being snapshotted.

## Run

```bash
uv pip install -e .
BACKUP_MCP_ROOT=~/.backup-mcp backup-mcp     # stdio transport
```

Claude Desktop / any MCP client config:

```json
{
  "mcpServers": {
    "backup-ops": {
      "command": "backup-mcp",
      "env": { "BACKUP_MCP_ROOT": "~/.backup-mcp" }
    }
  }
}
```

## Tests

```bash
uv pip install pytest anyio
pytest
```

The suite drives the server through a real MCP client, under both the 2026-07-28 protocol (`input_required` elicitation) and the legacy mid-call elicitation flow.
