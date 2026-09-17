"""Which models a validation run was measured on.

A pass rate is a fact about a scenario *and* the models that did the work. Until
Phase 10 only the first half was recorded, so changing the catalog left every
earlier run counting towards the release gate: the evidence that the platform
passed at 80% was evidence about models that were no longer the ones running,
and a cheaper default could lower a scenario below its threshold with the gate
still reading green.

So every run records the routing profile it ran under - which catalog entry each
kind of work goes to - as a fingerprint. The gate reads only runs under the
profile the machine has now; a changed profile starts with no evidence and has
to earn it. A *baseline* run is one where every kind of work was forced onto the
strongest model in the catalog, and is what a cheaper profile is compared with.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RoutingProfile:
    fingerprint: str
    #: Task kind -> "entry (provider/model)", as the router answers it now.
    routes: dict[str, str] = field(default_factory=dict)
    baseline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "routes": dict(self.routes),
            "baseline": self.baseline,
        }

    @classmethod
    def from_dict(cls, raw: object) -> RoutingProfile | None:
        if not isinstance(raw, dict) or not raw.get("fingerprint"):
            return None
        routes = raw.get("routes") if isinstance(raw.get("routes"), dict) else {}
        return cls(
            fingerprint=str(raw["fingerprint"]),
            routes={str(k): str(v) for k, v in routes.items()},
            baseline=bool(raw.get("baseline", False)),
        )


def profile_of(
    routes: Mapping[str, str],
    *,
    models: Mapping[str, str] | None = None,
    baseline: bool = False,
) -> RoutingProfile:
    """A profile whose fingerprint changes when a route or a routed model does.

    `models` is every catalog entry's contract in a line. It is part of the
    fingerprint and not of the record: a route that still names `fast` while
    `fast` now points at a different model is a different profile, and a
    quality floor that moves work between entries is one too.
    """
    canonical = json.dumps(
        {"routes": dict(routes), "models": dict(models or {})}, sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return RoutingProfile(fingerprint=digest, routes=dict(routes), baseline=baseline)
