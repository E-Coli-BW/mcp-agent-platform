import pytest

from app.runtime.checkpoint import SQLiteCheckpointStore
from app.runtime.continuation import ContinuationWorker


@pytest.mark.asyncio
async def test_async_continuation_persists_tool_result(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(run_id="run-1", step_seq=1, state="approved", tool_name="code_shell")
    result = await ContinuationWorker(store, "worker-a").run_once_async(
        "run-1", lambda checkpoint: _result(checkpoint)
    )
    assert result.state == "completed"
    assert result.payload["result"] == "ok"


async def _result(_checkpoint):
    return "ok"
