"""The accounts subprocess is fake, isolated, rate limited and nonblocking."""

import asyncio
import json

from codex_worker_board import accounts as accounts_module
from codex_worker_board.accounts import Accounts


def write_script(path, body):
    path.write_text(
        "import sys\nassert sys.flags.isolated\n"
        "assert sys.argv[1:] == ['accounts', '--json']\n" + body
    )


def test_accounts_success_and_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TASK_HOME", str(tmp_path))
    script = tmp_path / "accounts.py"
    marker = tmp_path / "calls"
    payload = {"accounts": [{"name": "fake", "remaining": 42}], "extra": [None, True]}
    write_script(script, f"from pathlib import Path\nPath({str(marker)!r}).open('a').write('call\\n')\n"
                 f"print({json.dumps(payload)!r})\n")

    async def check():
        accounts = Accounts(script, 300)
        assert accounts.value == {"fetched_at": None, "error": None, "data": None}
        result = await accounts.refresh()
        assert result["data"] == payload
        assert result["error"] is None
        assert isinstance(result["fetched_at"], float)
        assert await accounts.refresh() is None
        assert marker.read_text().splitlines() == ["call"]

    asyncio.run(check())


def test_accounts_error_and_timeout_keep_previous_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_TASK_HOME", str(tmp_path))
    script = tmp_path / "accounts.py"
    write_script(script, "print('{\"cached\": [1, 2]}')\n")

    async def check():
        accounts = Accounts(script, 0)
        first = await accounts.refresh()
        assert first["error"] is None
        write_script(script, "print('fake accounts failure', file=sys.stderr)\nsys.exit(7)\n")
        failed = await accounts.refresh()
        assert failed["error"] == "fake accounts failure"
        assert failed["data"] == first["data"]
        monkeypatch.setattr(accounts_module, "ACCOUNTS_TIMEOUT", 0.2)
        write_script(script, "import time\ntime.sleep(30)\n")
        pending = asyncio.create_task(accounts.refresh())
        # A subprocess waiting for timeout must leave the event loop responsive.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        assert not pending.done()
        timed_out = await asyncio.wait_for(pending, timeout=10)
        assert "timed out" in timed_out["error"]
        assert "0.2" in timed_out["error"]
        assert timed_out["data"] == first["data"]

    asyncio.run(check())
