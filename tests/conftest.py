"""Shared test fixtures / import shims.

Importing ``custom_components.eisenberg.camera`` pulls in
``homeassistant.components.camera``, whose ``img_util`` imports PyTurboJPEG —
a binding over the system ``libturbojpeg`` C library. That library is
irrelevant to the logic under test (stream_source URL shaping) and requiring
it would drag a system dependency into the test environment. Stub it so the
camera platform imports cleanly; the real module is never exercised here.
"""

from __future__ import annotations

import sys
from types import ModuleType


def _stub_turbojpeg() -> None:
    if "turbojpeg" in sys.modules:
        return
    module = ModuleType("turbojpeg")
    # img_util only needs the names to exist at import time.
    module.TurboJPEG = object  # type: ignore[attr-defined]
    module.TJFLAG_FASTUPSAMPLE = 0  # type: ignore[attr-defined]
    module.TJFLAG_FASTDCT = 0  # type: ignore[attr-defined]
    sys.modules["turbojpeg"] = module


_stub_turbojpeg()
