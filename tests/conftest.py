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
    from yuyu import usage

    throwaway = tmp_path_factory.mktemp("usage-history")
    with patch.object(usage, "USAGE_DIR", throwaway):
        yield throwaway
