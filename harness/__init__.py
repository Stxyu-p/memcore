# memcore harness — Phase 0 evaluation suite
# Runs today against fixtures. Tests that need the core engine are expected-fail.
#
# Test modules import their siblings by bare name (`import telemetry_test_support`),
# which only resolves when the harness dir is on sys.path — true for bare
# `discover -s harness`, false for `-t .` (package import). Put it there ourselves
# so both invocation styles work.
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
