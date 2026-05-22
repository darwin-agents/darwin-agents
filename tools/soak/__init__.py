"""Phase 0 soak test (docs/engineering/20 §"The soak test").

The soak test is Phase 0's acceptance gate. It runs a real Darwin
against a live platform endpoint for a configured duration and asserts
the roadmap's six definition-of-done criteria:

  1. The Darwin spawns and runs for the whole duration without crashing.
  2. >= 50 neuron dispatches at >= 80% success rate.
  3. Every action is in the event log with a valid hash chain.
  4. The ledger reconciles against the wallet with zero discrepancy.
  5. A kill-switch flip halts the Darwin in < 5 seconds.
  6. Any moment of the Darwin's life replays from the event log.

The harness drives an *already-running* endpoint over HTTP (the
operator starts ``darwin-platform serve`` separately). It is
duration-parameterised: ``--duration 24h`` is the real Phase-0 gate,
``--duration 2m`` is a CI smoke run over the same code path.
"""
