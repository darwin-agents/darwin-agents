"""Operational drills (docs/engineering/09 §operational drills).

Scripted exercises for the safety-critical procedures: kill-switch,
DR failover, Vault seal/unseal. The point is to practice them in a
controlled drill *before* the moment they're needed for real, so the
operator at 3am isn't reading docs.

Each drill is a stand-alone CLI module under ``tools.drills``; see the
README for usage + verification status.
"""
