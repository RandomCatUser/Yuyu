"""Shared pytest fixtures / path setup for the ported suite."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _keep_usage_history_out_of_the_real_folder(tmp_path_factory):
    """Send usage recording to a throwaway folder for the whole run.

    Any test that reaches the provider helpers writes one line of token
    accounting per call, because that is what the helpers now do. Left alone, a
    passing run would drop fake rows into the real usage/*.jsonl - zeros from
    mocked responses, every one of them unpriced - which the dashboard would
    then carry forever as unpriced calls and a cost total stuck at a floor.

    Done here rather than per test file so a new test cannot reintroduce it.
    """
    from yuyu import usage

    throwaway = tmp_path_factory.mktemp("usage-history")
    with patch.object(usage, "USAGE_DIR", throwaway):
        yield throwaway
