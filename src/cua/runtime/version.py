"""Detect the running app version (for compatibility checks and drift detection)."""

from __future__ import annotations

import re

from cua.artifact.profile import AppProfile
from cua.surface.base import Surface


async def probe_version(surface: Surface, profile: AppProfile) -> str | None:
    probe = profile.spec.version_probe
    if probe is None:
        return None
    state = await surface.state()
    for frame in state.frames:
        if probe.frame is not None and frame.name != probe.frame.name:
            continue
        match = re.search(probe.pattern, frame.text)
        if match:
            return match.group("version")
    return None
