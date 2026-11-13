"""Stage E: the live e2e script must only ever touch the account's own content."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e_live.py"


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("e2e_live", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reply_target_is_only_own_comment(e2e):
    comments = [
        {"id": "c1", "username": "customer_a", "replies": [{"id": "r1", "username": "customer_b"}]},
        {"id": "c2", "username": "customer_c", "replies": [{"id": "r2", "username": "paula_tf"}]},
        {"id": "c3", "username": "paula_tf"},
    ]
    assert e2e.pick_own_comment(comments, "paula_tf") in ("c3", "r2")
    assert e2e.pick_own_comment([{"id": "c1", "username": "customer"}], "paula_tf") is None
    assert e2e.pick_own_comment([], "paula_tf") is None


def test_dm_phase_requires_explicit_recipient(e2e):
    with pytest.raises(SystemExit):
        e2e.main(["--server", "x", "--phases", "dm", "--approve"])


def test_no_hardcoded_account_ids(e2e):
    text = SCRIPT.read_text()
    assert "17841401555704149" not in text and '"vivefit"' not in text and "bnancore" not in text
