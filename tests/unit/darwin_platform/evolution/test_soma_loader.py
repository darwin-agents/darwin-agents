"""Boot-time soma activation (docs/engineering/33).

``SomaLoader`` is the read-side twin of ``SomaMerger``: it loads a Darwin's
evolved ``soma/strategies/`` as a runnable overlay so self-modification
actually changes behaviour. It re-validates independently (path sandbox + AST
guard) and fails CLOSED — a bad/absent/tampered soma yields no overlay so the
runtime keeps ``founding_strategy()``.
"""

from __future__ import annotations

from pathlib import Path

from darwin_dna.strategies import Strategy
from darwin_platform.evolution import SomaLoader

# A valid evolved strategy: the real relative imports the shipped strategy
# file uses (``._base`` / ``..neurons``) + a behavioural tweak.
_VALID = '''
from ._base import StrategyAction, StrategyObservation, StrategySpec
from ..neurons import Task


class EvolvedStrategy:
    _spec = StrategySpec(
        id="evolved_v2", archetype="content.affiliate",
        neurons_used=frozenset({"content.writer"}),
        required_capabilities=frozenset(),
        fitness_signals=["affiliate_revenue_usdc"],
        est_daily_cost_usdc=0.25, revenue_cadence_days=7.0,
    )

    @property
    def spec(self):
        return self._spec

    def propose(self, obs):
        return StrategyAction(kind="wait", reason="evolved: patience")


def founding_strategy():
    return EvolvedStrategy()
'''


def _seed(root: Path, name: str, body: str) -> Path:
    d = root / "soma" / "strategies"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)
    return d / name


def test_loads_valid_evolved_strategy(tmp_path):
    _seed(tmp_path, "content_affiliate.py", _VALID)
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is True
    assert isinstance(res.strategy, Strategy)
    assert res.strategy.spec.id == "evolved_v2"
    assert res.source.endswith("content_affiliate.py")
    assert res.digest  # fingerprint ties the running code to forensics


def test_no_overlay_without_strategies_dir(tmp_path):
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False
    assert res.strategy is None


def test_rejects_dangerous_import(tmp_path):
    _seed(tmp_path, "content_affiliate.py", "import os\nos.system('x')\n")
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False
    assert res.strategy is None
    assert "disallowed import" in res.reason


def test_rejects_reaching_into_genome_core(tmp_path):
    # Only the strategy contract (._base / ..neurons) is importable — never
    # the kill switch / machinery / validator (Layer-0 stays static, P1).
    _seed(
        tmp_path, "content_affiliate.py",
        "from ..machinery.kill_switch import KillSwitch\n",
    )
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False
    assert "disallowed relative import" in res.reason


def test_rejects_dunder_escape(tmp_path):
    # The classic ().__class__.__bases__[0].__subclasses__() sandbox escape.
    _seed(
        tmp_path, "content_affiliate.py",
        "from ._base import StrategySpec\nLEAK = ().__class__.__bases__\n",
    )
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False
    assert "dunder" in res.reason


def test_rejects_forbidden_builtin(tmp_path):
    _seed(
        tmp_path, "content_affiliate.py",
        "from ._base import StrategySpec\nX = eval('1+1')\n",
    )
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False
    assert "forbidden name" in res.reason


def test_fails_closed_on_syntax_error(tmp_path):
    _seed(tmp_path, "content_affiliate.py", "def f(:\n  pass\n")
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False
    assert res.strategy is None


def test_skips_private_modules(tmp_path):
    # A leading-underscore module is a helper, not an entry point: never run.
    _seed(tmp_path, "_helpers.py", "import os\n")  # would fail AST if loaded
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False  # the dangerous helper was simply skipped


def test_no_overlay_when_no_strategy_object(tmp_path):
    # A syntactically/AST-valid module that exposes no Strategy → no overlay.
    _seed(
        tmp_path, "content_affiliate.py",
        "from ._base import StrategySpec\nVALUE = 7\n",
    )
    res = SomaLoader(tmp_path).load_strategy()
    assert res.loaded is False
    assert res.strategy is None
