"""Runner adapters — port of ``testing/runner.ts``.

Upstream ``registerEnvConformance`` registers the cases with a Vitest/Jest runner
(``describe``/``it``/``expect``). pytest collects tests at import time, so the
Python adapter is a parametrization source instead: call
:func:`env_conformance_cases` at module level and hand the result to
``pytest.mark.parametrize``. The case set, names, bodies and timeouts are identical.
"""

from __future__ import annotations

from collections.abc import Sequence

from .assertions import PytestAssertions
from .env_conformance import create_env_conformance
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

__all__ = ["conformance_ids", "env_conformance_cases", "storage_conformance_cases"]


def env_conformance_cases(
    with_env: EnvConformanceProvider,
    *,
    assertions: EnvConformanceAssertions | None = None,
    shell: tuple[str, ...] = ("sh", "-c"),
    symlinks: bool = True,
    list_files: bool = False,
) -> tuple[EnvConformanceCase, ...]:
    """The environment cases for ``with_env``, ready for ``pytest.mark.parametrize``.

    The counterpart of upstream ``registerEnvConformance(runner, name, withEnv, options)``.
    """
    return create_env_conformance(
        EnvConformanceOptions(
            assertions=assertions if assertions is not None else PytestAssertions(),
            with_env=with_env,
            shell=shell,
            symlinks=symlinks,
            list_files=list_files,
        )
    )


def conformance_ids(cases: Sequence[EnvConformanceCase | StorageConformanceCase]) -> list[str]:
    """Stable pytest ids: the upstream case names."""
    return [case.name for case in cases]


def storage_conformance_cases(
    with_storage: StorageConformanceProvider,
    *,
    assertions: StorageConformanceAssertions | None = None,
) -> tuple[StorageConformanceCase, ...]:
    """The ``Storage`` cases for ``with_storage``, ready for ``pytest.mark.parametrize``.

    The counterpart of upstream ``registerStorageConformance(runner, name, withStorage)``.
    """
    return create_storage_conformance(
        StorageConformanceOptions(
            assertions=assertions if assertions is not None else PytestAssertions(),
            with_storage=with_storage,
        )
    )
