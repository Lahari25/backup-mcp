import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import ElicitResult

from backup_mcp.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "data"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "sub" / "b.txt").write_text("beta")
    return src


@pytest.fixture
def server(tmp_path: Path):
    return build_server(tmp_path / "store")


class Answers:
    """Elicitation callback that records each question and replies with `reply`."""

    def __init__(self, confirm: bool | None = True) -> None:
        self.confirm = confirm
        self.questions: list[str] = []

    async def __call__(self, context, params):
        self.questions.append(params.message)
        if self.confirm is None:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"confirm": self.confirm})


@pytest.fixture(params=["auto", "legacy"])
def connect(request, server):
    """Connect over the 2026-07-28 input_required flow ("auto") and the legacy mid-call request flow."""

    def _connect(answers: Answers | None = None):
        return Client(server, mode=request.param, elicitation_callback=answers or Answers())

    return _connect


async def call(client, name, **args):
    result = await client.call_tool(name, args)
    return result


async def test_tools_are_listed_with_output_schemas(connect):
    async with connect() as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "create_snapshot",
        "list_snapshots",
        "diff_snapshot",
        "verify_snapshot",
        "restore_snapshot",
    }
    assert tools["diff_snapshot"].output_schema is not None
    assert tools["list_snapshots"].annotations.read_only_hint is True


async def test_snapshot_diff_verify_restore_roundtrip(connect, source, tmp_path):
    async with connect() as client:
        created = await call(client, "create_snapshot", source=str(source), label="before")
        assert not created.is_error, created.content
        info = created.structured_content
        assert info["file_count"] == 2 and info["label"] == "before"
        sid = info["snapshot_id"]

        (source / "a.txt").write_text("changed")
        (source / "c.txt").write_text("new")
        (source / "sub" / "b.txt").unlink()

        diff = (await call(client, "diff_snapshot", snapshot_id=sid)).structured_content
        assert diff == {
            "snapshot_id": sid,
            "added": ["c.txt"],
            "removed": ["sub/b.txt"],
            "modified": ["a.txt"],
            "unchanged_count": 0,
        }

        assert (await call(client, "verify_snapshot", snapshot_id=sid)).structured_content["ok"] is True

        target = tmp_path / "restored"
        restored = await call(client, "restore_snapshot", snapshot_id=sid, target=str(target))
        assert not restored.is_error, restored.content
        assert (target / "a.txt").read_text() == "alpha"
        assert (target / "sub" / "b.txt").read_text() == "beta"


async def test_list_snapshots_returns_structured_list(connect, source):
    async with connect() as client:
        await call(client, "create_snapshot", source=str(source))
        listed = await call(client, "list_snapshots", source=str(source))
    assert not listed.is_error
    assert len(listed.structured_content["result"]) == 1


async def test_restore_refuses_non_empty_target_and_traversal(connect, source, tmp_path):
    async with connect() as client:
        sid = (await call(client, "create_snapshot", source=str(source))).structured_content["snapshot_id"]
        busy = tmp_path / "busy"
        busy.mkdir()
        (busy / "keep.txt").write_text("x")
        refused = await call(client, "restore_snapshot", snapshot_id=sid, target=str(busy))
        assert refused.is_error and "must be empty" in refused.content[0].text
        assert (busy / "keep.txt").read_text() == "x"

        sneaky = await call(
            client, "restore_snapshot", snapshot_id=sid, target=str(tmp_path / "t"), files=["../a.txt"]
        )
        assert sneaky.is_error and "invalid file path" in sneaky.content[0].text


async def test_verify_detects_corruption_and_restore_refuses_it(connect, source, tmp_path):
    async with connect() as client:
        sid = (await call(client, "create_snapshot", source=str(source))).structured_content["snapshot_id"]
        (tmp_path / "store" / sid / "files" / "a.txt").write_text("bitrot")
        verify = (await call(client, "verify_snapshot", snapshot_id=sid)).structured_content
        assert verify["ok"] is False and verify["corrupted"] == ["a.txt"]
        refused = await call(client, "restore_snapshot", snapshot_id=sid, target=str(tmp_path / "t"))
        assert refused.is_error and "failed verification" in refused.content[0].text
        partial = await call(
            client, "restore_snapshot", snapshot_id=sid, target=str(tmp_path / "t2"), files=["sub/b.txt"]
        )
        assert not partial.is_error


async def test_unknown_snapshot_is_a_tool_error(connect):
    async with connect() as client:
        result = await call(client, "diff_snapshot", snapshot_id="nope")
    assert result.is_error and "snapshot not found" in result.content[0].text


async def test_manifest_resource_and_prompt(connect, source):
    async with connect() as client:
        sid = (await call(client, "create_snapshot", source=str(source))).structured_content["snapshot_id"]
        res = await client.read_resource(f"snapshot://{sid}/manifest")
        manifest = json.loads(res.contents[0].text)
        assert set(manifest["files"]) == {"a.txt", "sub/b.txt"}
        prompt = await client.get_prompt("plan_restore", {"snapshot_id": sid, "incident": "deleted config"})
        assert sid in prompt.messages[0].content.text


@pytest.mark.parametrize("answers", [Answers(confirm=False), Answers(confirm=None)], ids=["no", "declined"])
async def test_restore_writes_nothing_without_confirmation(connect, source, tmp_path, answers):
    async with connect(answers) as client:
        sid = (await call(client, "create_snapshot", source=str(source))).structured_content["snapshot_id"]
        target = tmp_path / "t"
        result = await call(client, "restore_snapshot", snapshot_id=sid, target=str(target))
    assert result.is_error and "not confirmed" in result.content[0].text
    assert not target.exists()


async def test_restore_asks_with_the_file_count(connect, source, tmp_path):
    answers = Answers()
    async with connect(answers) as client:
        sid = (await call(client, "create_snapshot", source=str(source))).structured_content["snapshot_id"]
        await call(client, "restore_snapshot", snapshot_id=sid, target=str(tmp_path / "t"), files=["a.txt"])
    assert answers.questions == [f"Restore 1 file(s) from snapshot {sid} into {tmp_path / 't'}?"]
