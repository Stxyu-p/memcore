"""
MemCore — core engine facade.

Unified facade re-exporting write, recall, doctor, and import engines,
preserving 100% backward compatibility for all callers and test harnesses.
"""
from __future__ import annotations

import sys
import types
from typing import Any

from . import write as _write
from . import recall as _recall
from . import doctor as _doctor
from . import imports as _imports


class _CoreFacade(types.ModuleType):
    """Module proxy that synchronizes attribute updates with submodules."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for m in (_write, _recall, _doctor, _imports):
            if hasattr(m, name):
                setattr(m, name, value)


_mod = _CoreFacade(__name__, __doc__)

# Populate facade with all attributes from submodules
for _m in (_write, _recall, _doctor, _imports):
    for _k, _v in _m.__dict__.items():
        if not _k.startswith('__'):
            super(_CoreFacade, _mod).__setattr__(_k, _v)

_mod.__all__ = [k for k in dir(_mod) if not k.startswith('_')]
_mod.__file__ = __file__
_mod.__package__ = __package__
_mod.__doc__ = __doc__

sys.modules[__name__] = _mod
