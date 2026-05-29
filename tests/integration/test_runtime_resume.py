"""Tests for runtime resume after a supervisor restart (doc 08 §HA).

These exercise the persistent lifecycle table + the Supervisor's
``ensure_leader_started`` path that re-spawns each alive Darwin.

We model a "process restart" by closing the first Supervisor and opening
a second against the same DB file. The audit chain + lineage edges +
billing carry across; the runtime is fresh."""

from __future__ import annotations


import pytest

from darwin_platform.runtime.supervisor import Supervisor


@pytest.mark.asyncio
async def test_lifecycle_persisted_on_spawn(tmp_path):
    db = tmp_path / "platform.db"
    sup = Supervisor(persistence_path=str(db))
    status = await sup.spawn(opening_usdc=100.0)
    did = status.darwin_id
    # Check the persistent lifecycle store recorded the spawn.
    rec = sup._lifecycle.get(did)
    assert rec is not None
    assert rec.alive is True
    assert rec.killed is False
    assert rec.opening_usdc == 100.0
    await sup.shutdown(kill_running=False)
    sup._persistence.close()


@pytest.mark.asyncio
async def test_lifecycle_marks_kill(tmp_path):
    db = tmp_path / "platform.db"
    sup = Supervisor(persistence_path=str(db))
    status = await sup.spawn(opening_usdc=100.0)
    await sup.kill(status.darwin_id, reason="test-kill")
    rec = sup._lifecycle.get(status.darwin_id)
    assert rec.alive is False
    assert rec.killed is True
    assert rec.kill_reason == "test-kill"
    await sup.shutdown(kill_running=False)
    sup._persistence.close()


@pytest.mark.asyncio
async def test_resume_re_spawns_alive_darwins(tmp_path):
    """Spawn a Darwin, close the supervisor, open a fresh one against
    the same DB. The fresh supervisor's ``ensure_leader_started`` should
    re-spawn the alive Darwin."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    await sup_a.spawn(opening_usdc=50.0)
    await sup_a.spawn(opening_usdc=75.0)
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    # Fresh supervisor — same DB.
    sup_b = Supervisor(persistence_path=str(db))
    # ensure_leader_started runs the resume.
    await sup_b.ensure_leader_started()
    # Two new running Darwins (with fresh ids; the audit log records
    # the link via the resume entries).
    assert len(sup_b._darwins) == 2
    resume_entries = sup_b._audit.entries(action="resume")
    assert len(resume_entries) == 2
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_resume_skips_killed_darwins(tmp_path):
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status1 = await sup_a.spawn(opening_usdc=50.0)
    await sup_a.spawn(opening_usdc=75.0)
    await sup_a.kill(status1.darwin_id, reason="dev-shutdown")
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    # Only the unkilled Darwin is re-spawned.
    assert len(sup_b._darwins) == 1
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_resume_idempotent(tmp_path):
    """Calling ensure_leader_started twice does not double-resume."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    await sup_a.spawn(opening_usdc=50.0)
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    n1 = len(sup_b._darwins)
    # The persisted Darwin is now ALSO recorded under the new id; calling
    # ensure_leader_started again should be a no-op.
    await sup_b.ensure_leader_started()
    assert len(sup_b._darwins) == n1
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_no_persistence_means_no_lifecycle(tmp_path):
    """Without persistence, the lifecycle store is None and resume is
    a no-op (which is the right behaviour for ephemeral dev)."""
    sup = Supervisor()
    await sup.spawn(opening_usdc=50.0)
    assert sup._lifecycle is None
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_lossless_resume_keeps_same_id_and_audit_chain(tmp_path):
    """The doc-08 §HA promise: a resumed Darwin keeps its original id
    AND its audit chain continues from the previous tip — not a fresh
    chain. The manifest is re-mintable via the platform signing key
    that was also persisted."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status_a = await sup_a.spawn(opening_usdc=50.0)
    original_id = status_a.darwin_id
    # Read the audit chain tip from before the restart.
    handle_a = sup_a._darwins[original_id]
    pre_records = handle_a.runtime.audit.read_records()
    pre_tip_hash = pre_records[-1]["hash"]
    pre_len = len(pre_records)
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    # Restart.
    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    # The SAME id is back — not a fresh genesis.
    assert original_id in sup_b._darwins
    handle_b = sup_b._darwins[original_id]
    # The audit chain continues — the first records match the prior tip.
    post_records = handle_b.runtime.audit.read_records()
    assert len(post_records) >= pre_len
    # The prefix is byte-equal: every record we wrote before the restart
    # is present in the resumed chain.
    for i in range(pre_len):
        assert post_records[i]["hash"] == pre_records[i]["hash"]
    # The pre-restart tip hash is the prev_hash of whatever record came
    # next (the resume event), confirming linkage.
    if len(post_records) > pre_len:
        assert post_records[pre_len]["prev_hash"] == pre_tip_hash

    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_resume_audit_records_lossless_flag(tmp_path):
    """The platform's operator audit records the lossless=True meta on
    resume so a compliance reviewer can distinguish lossless resumes
    from lossy fallbacks."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    await sup_a.spawn(opening_usdc=50.0)
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    resume_entries = sup_b._audit.entries(action="resume")
    assert len(resume_entries) == 1
    assert resume_entries[0].metadata["lossless"] is True

    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_quarantine_state_survives_restart(tmp_path):
    """A quarantined Darwin (doc-09 level 4) must come back from a
    supervisor restart still quarantined AND paused (doc 23) — it must not
    silently slip back to running unsupervised."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status = await sup_a.spawn(opening_usdc=80.0)
    did = status.darwin_id
    await sup_a.quarantine(did, reason="policy breach", operator="trust-team")
    rec = sup_a._lifecycle.get(did)
    assert rec.quarantined is True
    assert rec.quarantine_reason == "policy breach"
    assert rec.quarantine_operator == "trust-team"
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    assert did in sup_b._darwins
    h = sup_b._darwins[did]
    assert h.quarantine.active is True
    assert h.quarantine.reason == "policy breach"
    assert h.quarantine.operator == "trust-team"
    # Quarantine implies paused — the resumed genome is parked, not free.
    assert h.stack.pause.is_paused() is True
    # The restore is recorded for a compliance reviewer.
    assert len(sup_b._audit.entries(action="quarantine_restored")) == 1
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_unquarantine_clears_persisted_state(tmp_path):
    """Clearing quarantine before a restart means the Darwin resumes
    normally — the persisted flag is cleared too."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status = await sup_a.spawn(opening_usdc=80.0)
    did = status.darwin_id
    await sup_a.quarantine(did, reason="false alarm", operator="trust-team")
    await sup_a.unquarantine(did, operator="trust-team")
    rec = sup_a._lifecycle.get(did)
    assert rec.quarantined is False
    assert rec.quarantine_reason is None
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    h = sup_b._darwins[did]
    assert h.quarantine.active is False
    assert sup_b._audit.entries(action="quarantine_restored") == []
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_v2_db_upgrades_to_quarantine_columns(tmp_path):
    """An existing v2 database — darwin_lifecycle without the quarantine
    columns — upgrades cleanly: the v3 migration ALTERs the columns in."""
    import sqlite3

    db = tmp_path / "platform.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE darwin_lifecycle ("
        " darwin_id TEXT PRIMARY KEY, opening_usdc REAL NOT NULL,"
        " last_balance REAL, parent_id TEXT, spawned_at REAL NOT NULL,"
        " alive INTEGER NOT NULL, killed INTEGER NOT NULL,"
        " kill_reason TEXT, escalation_auto TEXT);"
        "CREATE TABLE schema_version ("
        " id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL,"
        " applied_at REAL NOT NULL, code_version TEXT NOT NULL);"
        "INSERT INTO schema_version VALUES (1, 2, 0.0, 'test-v2');"
    )
    conn.commit()
    conn.close()

    # Opening with the current code runs every pending migration in
    # place — including v3 (quarantine columns) and v4 (pre-approvals).
    sup = Supervisor(persistence_path=str(db))
    try:
        from darwin_platform.runtime.persistence import CURRENT_SCHEMA_VERSION
        assert sup._persistence.schema_version() == CURRENT_SCHEMA_VERSION
        status = await sup.spawn(opening_usdc=70.0)
        await sup.quarantine(
            status.darwin_id, reason="upgraded", operator="ops"
        )
        rec = sup._lifecycle.get(status.darwin_id)
        assert rec.quarantined is True
        assert rec.quarantine_reason == "upgraded"
    finally:
        await sup.shutdown(kill_running=False)
        sup._persistence.close()


@pytest.mark.asyncio
async def test_signing_key_persists_across_restart(tmp_path):
    """The platform signing key is persisted so manifests signed before
    the restart still verify after."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    pem_a = sup_a.signing_public_key_pem()
    await sup_a.shutdown(kill_running=True)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    pem_b = sup_b.signing_public_key_pem()
    assert pem_a == pem_b

    await sup_b.shutdown(kill_running=True)
    sup_b._persistence.close()


# ---------------------------------------------------------------------------
# Pre-approval persistence (doc 17 §"Darwin paying a human")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preapprovals_persist_across_restart(tmp_path):
    """Owner installs a pre-approval rule. After a supervisor restart
    the resumed Darwin's wallet still holds the rule, so the genome's
    facilitator still auto-allows matching spends without escalating."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status = await sup_a.spawn(opening_usdc=100.0, actor="alice")
    did = status.darwin_id
    await sup_a.set_preapproval(
        did,
        rule_id="upwork-under-50",
        max_amount_usdc=50.0,
        counterparty_category="freelancer",
        counterparty_substring="upwork",
        actor="alice",
    )
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    # Find the resumed Darwin (id is preserved via the manifest store).
    assert did in sup_b._darwins
    wallet = sup_b._darwins[did].stack.wallet
    rules = wallet.owner_preapprovals()
    assert len(rules) == 1
    assert rules[0].rule_id == "upwork-under-50"
    assert rules[0].max_amount_usdc == 50.0
    assert rules[0].counterparty_substring == "upwork"
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_revoked_preapproval_does_not_resurrect(tmp_path):
    """A revoked rule must stay revoked across a restart — otherwise
    revoke is a temporary undo, which is the wrong contract."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status = await sup_a.spawn(opening_usdc=100.0, actor="alice")
    did = status.darwin_id
    await sup_a.set_preapproval(
        did, rule_id="r1", max_amount_usdc=50.0,
        counterparty_category="freelancer", actor="alice",
    )
    await sup_a.revoke_preapproval(did, "r1", actor="alice")
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    assert did in sup_b._darwins
    wallet = sup_b._darwins[did].stack.wallet
    assert wallet.owner_preapprovals() == ()
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_killed_darwin_preapprovals_purged(tmp_path):
    """Kill wipes the persisted rule rows so a future re-spawn of the
    same id can't accidentally resurrect the prior owner's grants."""
    db = tmp_path / "platform.db"
    sup = Supervisor(persistence_path=str(db))
    status = await sup.spawn(opening_usdc=100.0, actor="alice")
    did = status.darwin_id
    await sup.set_preapproval(
        did, rule_id="r1", max_amount_usdc=50.0,
        counterparty_category="freelancer", actor="alice",
    )
    await sup.kill(did, reason="test", actor="alice")
    persisted = sup._preapproval_store.load(darwin_id=did)
    assert persisted == []
    await sup.shutdown(kill_running=False)
    sup._persistence.close()


@pytest.mark.asyncio
async def test_preapproval_used_count_persists_across_restart(tmp_path):
    """A rule's used_count survives a supervisor restart so max_uses is
    enforced across the bounce — otherwise a restart would reset the
    counter and the owner's "up to 3 payments" budget would silently
    refill."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status = await sup_a.spawn(opening_usdc=100.0, actor="alice")
    did = status.darwin_id
    await sup_a.set_preapproval(
        did,
        rule_id="r1",
        max_amount_usdc=50.0,
        counterparty_category="freelancer",
        actor="alice",
    )
    # Simulate two consumes (the facilitator's hot path) — directly via
    # the wallet so we don't need a full payment flow here.
    wallet = sup_a._darwins[did].stack.wallet
    wallet.consume_preapproval("r1")
    wallet.consume_preapproval("r1")
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    wallet_b = sup_b._darwins[did].stack.wallet
    rules = wallet_b.owner_preapprovals()
    assert len(rules) == 1
    assert rules[0].used_count == 2
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_preapproval_max_uses_round_trips_through_persistence(tmp_path):
    """max_uses is a column on the rule, not just an in-memory field —
    it survives a restart so the engine can keep enforcing it."""
    db = tmp_path / "platform.db"
    sup_a = Supervisor(persistence_path=str(db))
    status = await sup_a.spawn(opening_usdc=100.0, actor="alice")
    did = status.darwin_id
    await sup_a.set_preapproval(
        did, rule_id="r1", max_amount_usdc=50.0,
        counterparty_category="freelancer", actor="alice",
    )
    # The supervisor's PUT signature doesn't yet take max_uses — patch
    # the rule directly via the wallet to simulate an admin extension.
    wallet = sup_a._darwins[did].stack.wallet
    from darwin_dna.constitution import PreApprovalRule
    wallet.set_preapproval(PreApprovalRule(
        rule_id="r1", max_amount_usdc=50.0,
        counterparty_category="freelancer", max_uses=3,
    ))
    # Sync through persistence so the restart finds the bumped shape.
    sup_a._preapproval_store.save(
        darwin_id=did, rule=wallet.owner_preapprovals()[0],
    )
    await sup_a.shutdown(kill_running=False)
    sup_a._persistence.close()

    sup_b = Supervisor(persistence_path=str(db))
    await sup_b.ensure_leader_started()
    rules = sup_b._darwins[did].stack.wallet.owner_preapprovals()
    assert rules[0].max_uses == 3
    assert rules[0].used_count == 0
    await sup_b.shutdown(kill_running=False)
    sup_b._persistence.close()


@pytest.mark.asyncio
async def test_v3_db_upgrades_through_v6_with_data(tmp_path):
    """A real v3 database — quarantine columns present, no pre-approvals
    or freelancer tables — upgrades cleanly through v4 (preapprovals
    table), v5 (freelancer_registry table), and v6 (max_uses / used_count
    columns on preapprovals).

    The point of this test (vs. the in-place v2 one above) is that it
    seeds actual ROWS in the pre-existing tables and verifies they
    survive the migration AND the new tables are immediately usable
    against the upgraded backend."""
    import sqlite3

    db = tmp_path / "platform.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        # darwin_lifecycle WITH quarantine columns (the v3 shape).
        "CREATE TABLE darwin_lifecycle ("
        " darwin_id TEXT PRIMARY KEY, opening_usdc REAL NOT NULL,"
        " last_balance REAL, parent_id TEXT, spawned_at REAL NOT NULL,"
        " alive INTEGER NOT NULL, killed INTEGER NOT NULL,"
        " kill_reason TEXT, escalation_auto TEXT,"
        " quarantined INTEGER NOT NULL DEFAULT 0,"
        " quarantine_reason TEXT, quarantine_operator TEXT,"
        " quarantine_applied_ts REAL);"
        "CREATE TABLE schema_version ("
        " id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL,"
        " applied_at REAL NOT NULL, code_version TEXT NOT NULL);"
        "INSERT INTO schema_version VALUES (1, 3, 0.0, 'test-v3');"
        # Seed a row that must survive the upgrade.
        "INSERT INTO darwin_lifecycle VALUES("
        " 'pre-existing', 42.0, 42.0, NULL, 1.0, 1, 0, NULL, 'approve',"
        " 0, NULL, NULL, NULL);"
    )
    conn.commit()
    conn.close()

    sup = Supervisor(persistence_path=str(db))
    try:
        from darwin_platform.runtime.persistence import CURRENT_SCHEMA_VERSION

        # All forward migrations applied.
        assert sup._persistence.schema_version() == CURRENT_SCHEMA_VERSION
        # Pre-existing v3 row is intact.
        rec = sup._lifecycle.get("pre-existing")
        assert rec is not None
        assert rec.opening_usdc == 42.0

        # v4 table: darwin_preapprovals is now usable.
        status = await sup.spawn(opening_usdc=100.0, actor="alice")
        await sup.set_preapproval(
            status.darwin_id,
            rule_id="r1",
            max_amount_usdc=50.0,
            counterparty_category="freelancer",
            actor="alice",
        )
        rules = sup._preapproval_store.load(darwin_id=status.darwin_id)
        assert len(rules) == 1

        # v5 table: freelancer_registry is now usable. Default in the
        # supervisor is the persistent store (auto-wired when persistence
        # is configured), so a direct call lands there.
        from darwin_platform.payments.freelancer_registry import (
            FreelancerDestination,
        )
        sup._freelancer_registry.register(FreelancerDestination(
            owner_subject="alice", freelancer_id="f1",
            fiat_destination="acct_x", display_name="One",
            kyc_status="verified",
            registered_at=0.0,
        ))
        listed = sup._freelancer_registry.list(owner_subject="alice")
        assert [d.freelancer_id for d in listed] == ["f1"]

        # v6 columns: max_uses + used_count present on the preapprovals
        # table — write a rule with max_uses + a consumed count and
        # round-trip it.
        from darwin_dna.constitution import PreApprovalRule
        sup._preapproval_store.save(
            darwin_id=status.darwin_id,
            rule=PreApprovalRule(
                rule_id="capped",
                counterparty_category="freelancer",
                max_amount_usdc=10.0,
                max_uses=5,
                used_count=2,
            ),
        )
        loaded = {
            r.rule_id: r
            for r in sup._preapproval_store.load(darwin_id=status.darwin_id)
        }
        assert loaded["capped"].max_uses == 5
        assert loaded["capped"].used_count == 2
    finally:
        await sup.shutdown(kill_running=False)
        sup._persistence.close()
