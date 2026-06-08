"""Boot-time soma activation — make a Darwin actually RUN its evolved soma
(docs/engineering/33, the read-side twin of :mod:`soma_merge`).

``SomaMerger`` writes promoted patches to ``<soma_root>/<id>/soma/strategies/``
on disk; until now nothing executed them, so self-modification changed
forensics + rollback state but never behaviour (the Phase-1 thesis, doc 21,
was unrealized). This module closes that: at genome boot the platform loads the
*validated* soma ``strategies/`` as an overlay and injects the resulting
:class:`~darwin_dna.strategies.Strategy` onto ``platform.strategy``; the runtime
prefers it over ``founding_strategy()`` (same optional-substrate seam as
``platform.holdings`` / ``platform.price_oracle``). With the soma now
PVC-persisted (k8s) a restart / respawn picks up the accumulated evolution.

Safety (doc 33 §"Safety model"), defence in depth below the per-Darwin
microVM + egress proxy + kill switch + treasury caps:

- **Layer-0 stays static.** Only ``soma/strategies/`` loads — never the genome
  core (kill switch, validator, audit, ledger, loop). P1 is absolute.
- **Sandboxed paths.** Every file is resolved under the soma root; a path that
  escapes is refused.
- **AST guard.** Each module is parsed and rejected if it imports anything
  outside a tiny allowlist (the strategy contract + safe stdlib), calls a
  dangerous builtin (``eval``/``exec``/``__import__``/``open``…), or touches a
  dunder attribute (the classic ``().__class__.__bases__`` escape).
- **Fail closed.** ANY validate/import/extract error skips that file and the
  loader falls back to ``founding_strategy()``; it never crashes boot and never
  half-loads. A tampered PVC reverts to the package default.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from darwin_dna.machinery.neuron_proposal import PATH_C_ALLOWED_TAGS
from darwin_dna.neurons import Neuron
from darwin_dna.strategies import Strategy

# Absolute imports a soma strategy may use — the strategy is a small,
# self-contained prior; anything beyond data/typing helpers is a red flag.
_ALLOWED_STDLIB = frozenset({
    "typing", "dataclasses", "math", "enum", "collections", "functools",
    "itertools", "re", "json", "datetime", "abc", "string", "statistics",
})
# Relative imports a soma strategy may use: the strategy contract (``._base``)
# and the neuron Task type (``..neurons``). Everything else under darwin_dna —
# machinery, kill switch, evolution, manifest, layers — is refused.
_ALLOWED_RELATIVE_TOP = frozenset({"_base", "neurons"})
# Builtins that enable code-exec / sandbox escape.
_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "globals", "locals",
    "vars", "input", "breakpoint", "memoryview", "getattr", "setattr",
    "delattr",
})


class SomaValidationError(Exception):
    """A soma module failed the independent AST / path re-validation."""


@dataclass(frozen=True)
class SomaLoadResult:
    """Outcome of a boot-time soma load. ``strategy`` is None when no valid
    overlay was found (the runtime then keeps ``founding_strategy()``).
    ``source``/``digest`` tie the running code to the forensic snapshot."""

    strategy: Strategy | None
    loaded: bool
    source: str | None
    digest: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "source": self.source,
            "digest": self.digest,
            "reason": self.reason,
        }


def _validate_ast(tree: ast.AST, *, path: str) -> None:
    """Refuse anything outside the strategy contract + safe stdlib. Best-effort
    defence in depth — NOT a complete Python sandbox (the real isolation is the
    microVM); it raises the bar against the obvious escapes."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _ALLOWED_STDLIB:
                    raise SomaValidationError(
                        f"{path}: disallowed import {alias.name!r}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:  # relative
                top = (node.module or "").split(".")[0]
                if top not in _ALLOWED_RELATIVE_TOP:
                    raise SomaValidationError(
                        f"{path}: disallowed relative import "
                        f"{'.' * node.level}{node.module or ''}"
                    )
            else:
                top = (node.module or "").split(".")[0]
                if top not in _ALLOWED_STDLIB:
                    raise SomaValidationError(
                        f"{path}: disallowed import from {node.module!r}"
                    )
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                raise SomaValidationError(
                    f"{path}: forbidden name {node.id!r}"
                )
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if len(attr) > 4 and attr.startswith("__") and attr.endswith("__"):
                raise SomaValidationError(
                    f"{path}: forbidden dunder attribute {attr!r}"
                )


class SomaLoader:
    """Load a Darwin's evolved soma ``strategies/`` as a runnable overlay.

    One instance per soma root (per Darwin), mirroring :class:`SomaMerger`.
    ``load_strategy`` never raises — a bad/absent/tampered soma yields
    ``strategy=None`` so the runtime falls back to ``founding_strategy()``.
    """

    def __init__(self, soma_root: str | Path) -> None:
        self._root = Path(soma_root).resolve()
        self._strategies_dir = self._root / "soma" / "strategies"

    @property
    def root(self) -> Path:
        return self._root

    def load_strategy(self) -> SomaLoadResult:
        """Return the first valid strategy overlay (files in sorted order), or
        an empty result. Each file is path-sandboxed, AST-validated, imported
        into a private module namespace, and the strategy extracted + checked
        against the :class:`Strategy` protocol — any failure skips the file."""
        if not self._strategies_dir.is_dir():
            return SomaLoadResult(None, False, None, "", "no soma strategies dir")
        last_reason = "no valid strategy overlay found"
        for path in sorted(self._strategies_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue  # private/helper modules are not entry points
            try:
                strategy, digest = self._load_one(path)
            except Exception as exc:  # noqa: BLE001 — fail closed, try next file
                last_reason = f"{path.name}: {type(exc).__name__}: {exc}"
                continue
            if strategy is not None:
                return SomaLoadResult(
                    strategy=strategy, loaded=True,
                    source=str(path), digest=digest,
                    reason="loaded soma strategy overlay",
                )
        return SomaLoadResult(None, False, None, "", last_reason)

    # -- helpers -----------------------------------------------------------
    def load_neurons(self) -> list[Neuron]:
        """Load validated soma-authored neurons from ``soma/neurons/*.py``.

        Each neuron must (a) pass the same path-sandbox + AST guard as a
        strategy, (b) structurally satisfy the :class:`Neuron` protocol, and
        (c) declare ``capability_tags`` that are a SUBSET of the Path-C
        allowlist — so a file-authored neuron is held to the same envelope as a
        Path-C structured proposal (never ``treasury.write``: the one-money-door
        invariant is preserved, and the runtime's ``load_catalog`` contract
        check re-asserts it when the overlay is merged). Returns the valid
        neurons (possibly empty); a bad file is skipped, never fatal.

        NOTE: the patch validator does NOT yet permit a Darwin to *write*
        ``soma/neurons/*.py`` (only Path-C structured proposals author neurons);
        this is the read side, ready for operator-placed or future-bridged
        neuron files."""
        out: list[Neuron] = []
        ndir = self._root / "soma" / "neurons"
        if not ndir.is_dir():
            return out
        for path in sorted(ndir.glob("*.py")):
            if path.name.startswith("_") or path.name.endswith("_client.py"):
                continue  # private helpers / neuron clients are not neurons
            try:
                out.extend(self._load_neurons_one(path))
            except Exception:  # noqa: BLE001 — fail closed, skip this file
                continue
        return out

    def _load_one(self, path: Path) -> tuple[Strategy | None, str]:
        abspath = path.resolve()
        if not self._within_root(abspath):
            raise SomaValidationError(f"{path}: escapes the soma sandbox")
        source = abspath.read_text()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        tree = ast.parse(source, filename=str(abspath))
        _validate_ast(tree, path=path.name)
        module = self._import_module(
            abspath, path.stem, digest, package="darwin_dna.strategies",
        )
        return self._extract_strategy(module), digest

    def _load_neurons_one(self, path: Path) -> list[Neuron]:
        abspath = path.resolve()
        if not self._within_root(abspath):
            raise SomaValidationError(f"{path}: escapes the soma sandbox")
        source = abspath.read_text()
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        tree = ast.parse(source, filename=str(abspath))
        _validate_ast(tree, path=path.name)
        module = self._import_module(
            abspath, path.stem, digest, package="darwin_dna.neurons",
        )
        found: list[Neuron] = []
        for name, val in vars(module).items():
            if name.startswith("_") or not isinstance(val, type):
                continue
            try:
                inst = val()
            except Exception:  # noqa: BLE001 — not a no-arg neuron class
                continue
            if not isinstance(inst, Neuron):
                continue
            tags = frozenset(getattr(inst, "capability_tags", frozenset()))
            if not tags or not tags <= PATH_C_ALLOWED_TAGS:
                # Empty or beyond the Path-C envelope (e.g. treasury.write) —
                # refuse the whole file, fail closed.
                raise SomaValidationError(
                    f"{path.name}: neuron {name!r} declares tags outside the "
                    f"Path-C allowlist: {sorted(tags - PATH_C_ALLOWED_TAGS)}"
                )
            found.append(inst)
        return found

    def _within_root(self, abspath: Path) -> bool:
        root = str(self._root)
        target = str(abspath)
        import os
        return target == root or target.startswith(root + os.sep)

    def _import_module(
        self, abspath: Path, stem: str, digest: str, *, package: str,
    ):
        # Name it a submodule of the genome ``package`` so the soma file's
        # relative imports (``._base`` / ``..neurons``) resolve, but with a
        # per-(root,file) suffix so two Darwins never collide or shadow the
        # real package module.
        tag = hashlib.sha256(
            (str(self._root) + digest).encode("utf-8")
        ).hexdigest()[:12]
        mod_name = f"{package}._soma_{tag}_{stem}"
        spec = importlib.util.spec_from_file_location(mod_name, str(abspath))
        if spec is None or spec.loader is None:
            raise SomaValidationError(f"{abspath}: cannot build import spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        return module

    def _extract_strategy(self, module) -> Strategy | None:
        # Prefer an explicit ``founding_strategy()`` factory (the convention
        # the shipped strategy file follows); else a public no-arg class that
        # structurally satisfies the Strategy protocol.
        factory = getattr(module, "founding_strategy", None)
        if callable(factory):
            obj = factory()
            if isinstance(obj, Strategy):
                return obj
        for name, val in vars(module).items():
            if name.startswith("_") or not isinstance(val, type):
                continue
            try:
                inst = val()
            except Exception:  # noqa: BLE001 — not a no-arg strategy class
                continue
            if isinstance(inst, Strategy):
                return inst
        return None
