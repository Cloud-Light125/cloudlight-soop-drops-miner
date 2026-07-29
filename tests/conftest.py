from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


# The checked-out directory has been renamed from ``soop_miner``.  Load its
# existing package metadata under the canonical import name for tests.
ROOT = Path(__file__).resolve().parents[1]
if "soop_miner" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "soop_miner",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["soop_miner"] = module
    spec.loader.exec_module(module)
