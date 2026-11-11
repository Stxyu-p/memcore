"""Redirect harness recall logs away from the user's live MemCore telemetry."""
import os
import tempfile

_TEST_DATA = tempfile.TemporaryDirectory(prefix="memcore-harness-telemetry-")
os.environ["MEMCORE_DATA_DIR"] = _TEST_DATA.name
