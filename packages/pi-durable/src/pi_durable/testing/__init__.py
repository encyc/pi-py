"""Runner-independent conformance suites.

Mirrors the subpath ``@earendil-works/pi-durable/testing``.

Two runner-independent suites, matching upstream:

* the ``ExecutionEnv`` half (``env-conformance.ts``) — batch A
* the ``Storage`` half (``storage-conformance.ts``) — batch B

``storage-benchmark.ts`` is still unported; see ``PORTING.md``.
"""

from __future__ import annotations

from .assertions import PytestAssertions, create_pytest_assertions
from .env_conformance import create_env_conformance
from .runner import conformance_ids, env_conformance_cases, storage_conformance_cases
from .storage_conformance import create_storage_conformance
from .types import (
    EnvConformanceAssertions,
    EnvConformanceCase,
    EnvConformanceOptions,
    EnvConformanceProvider,
    StorageConformanceAssertions,
    StorageConformanceCase,
    StorageConformanceOptions,
    StorageConformanceProvider,
)

__all__ = [
    "EnvConformanceAssertions",
    "EnvConformanceCase",
    "EnvConformanceOptions",
    "EnvConformanceProvider",
    "PytestAssertions",
    "StorageConformanceAssertions",
    "StorageConformanceCase",
    "StorageConformanceOptions",
    "StorageConformanceProvider",
    "conformance_ids",
    "create_env_conformance",
    "create_pytest_assertions",
    "create_storage_conformance",
    "env_conformance_cases",
    "storage_conformance_cases",
]
