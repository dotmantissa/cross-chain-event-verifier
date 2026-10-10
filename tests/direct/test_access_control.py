"""Access control and governance.

The split is deliberate and worth stating plainly:

  verification is permissionless by default, because consensus is what makes
  an answer trustworthy, not who asked for it. Anyone paying for a transaction
  may ask the network to verify something.

  registry writes are owner only, because a source list is a trust assumption.
  If anyone could add a source they control, they could supply a quorum of
  agreeing lies and defeat the whole design.

  reads are always open, including while paused, so a consumer contract can
  never be denied an answer it already paid to establish.
"""

import json

import pytest

from conftest import (
    TEST_CHAIN,
    addr_hex,
    TX_HASH,
    USDT,
    mock_all_agree,
    mock_llm_verdict,
)

NOW = "2026-01-15T10:30:00Z"

GOOD_SOURCES = json.dumps(
    [{"label": "one", "kind": "evm_jsonrpc", "url": "https://one.test.invalid/rpc"}]
)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_the_deployer_becomes_the_owner(verifier, direct_owner):
    assert verifier.get_config()["owner"].lower() == addr_hex(direct_owner)


@pytest.mark.parametrize(
    "method,args",
    [
        ("register_chain", ("5", "N", "evm", 5, 12, 10, 1, GOOD_SOURCES)),
        ("add_source", (TEST_CHAIN, "x", "evm_jsonrpc", "https://x.test.invalid", "")),
        ("remove_source", (TEST_CHAIN, "gamma")),
        ("set_chain_params", (TEST_CHAIN, 20, 5, 2)),
        ("set_chain_enabled", (TEST_CHAIN, False)),
        ("set_cache_policy", (100, 100, 100)),
        ("set_max_sources", (3,)),
        ("set_paused", (True,)),
        ("set_submitters_restricted", (True,)),
        ("purge_expired", (10,)),
    ],
)
def test_governance_is_owner_only(direct_vm, verifier, direct_bob, method, args):
    """Every state-changing administrative method is gated. A source list is a
    trust assumption, so letting a stranger edit it would be equivalent to
    letting them decide the answers."""
    direct_vm.sender = direct_bob
    with pytest.raises(Exception, match=r"\[EXPECTED\] owner only"):
        getattr(verifier, method)(*args)


def test_set_submitter_allowed_is_owner_only(direct_vm, verifier, direct_bob, direct_alice):
    direct_vm.sender = direct_bob
    with pytest.raises(Exception, match=r"\[EXPECTED\] owner only"):
        verifier.set_submitter_allowed(direct_alice, True)


def test_transfer_ownership_is_owner_only(direct_vm, verifier, direct_bob):
    direct_vm.sender = direct_bob
    with pytest.raises(Exception, match=r"\[EXPECTED\] owner only"):
        verifier.transfer_ownership(direct_bob)


def test_ownership_can_be_transferred(direct_vm, verifier, direct_owner, direct_bob):
    verifier.transfer_ownership(direct_bob)
    assert verifier.get_config()["owner"].lower() == addr_hex(direct_bob)

    # The new owner can govern.
    direct_vm.sender = direct_bob
    verifier.set_max_sources(2)
    assert verifier.get_config()["max_sources"] == 2

    # The old owner cannot.
    direct_vm.sender = direct_owner
    with pytest.raises(Exception, match=r"\[EXPECTED\] owner only"):
        verifier.set_max_sources(4)


def test_the_owner_may_also_verify(direct_vm, verifier, at_time, direct_owner):
    at_time(NOW)
    direct_vm.sender = direct_owner
    mock_all_agree(direct_vm)
    assert verifier.verify_transaction(TEST_CHAIN, TX_HASH)["confirmed"] is True


# ---------------------------------------------------------------------------
# Pausing
# ---------------------------------------------------------------------------


def test_pausing_stops_new_verifications(direct_vm, verifier, at_time):
    """The response to a discovered problem with the source set: stop writing
    new answers without destroying the ones already established."""
    at_time(NOW)
    verifier.set_paused(True)
    assert verifier.get_config()["paused"] is True

    for call in (
        lambda: verifier.verify_transaction(TEST_CHAIN, TX_HASH),
        lambda: verifier.refresh_transaction(TEST_CHAIN, TX_HASH),
        lambda: verifier.verify_claim(TEST_CHAIN, TX_HASH, "something"),
        lambda: verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT})),
    ):
        with pytest.raises(Exception, match=r"\[EXPECTED\] verifier is paused"):
            call()


def test_reads_keep_working_while_paused(direct_vm, verifier, at_time):
    """A consumer contract must never be denied an answer that is already
    established, or pausing would break every integration downstream."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    verifier.set_paused(True)
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True
    assert verifier.is_verified_strict(TEST_CHAIN, TX_HASH) is True
    assert verifier.get_verification(TEST_CHAIN, TX_HASH)["confirmed"] is True
    assert verifier.record_status(TEST_CHAIN, TX_HASH) == "success"
    assert verifier.list_chains()["count"] >= 1
    assert verifier.stats()["verifications"] == 1


def test_pausing_can_be_lifted(direct_vm, verifier, at_time):
    at_time(NOW)
    verifier.set_paused(True)
    verifier.set_paused(False)
    assert verifier.get_config()["paused"] is False

    mock_all_agree(direct_vm)
    assert verifier.verify_transaction(TEST_CHAIN, TX_HASH)["confirmed"] is True


def test_the_owner_cannot_verify_while_paused(direct_vm, verifier, at_time, direct_owner):
    """Pausing applies to everyone, including the owner. A pause that the owner
    can ignore is not a safety control."""
    at_time(NOW)
    direct_vm.sender = direct_owner
    verifier.set_paused(True)
    mock_all_agree(direct_vm)
    with pytest.raises(Exception, match=r"\[EXPECTED\] verifier is paused"):
        verifier.verify_transaction(TEST_CHAIN, TX_HASH)


# ---------------------------------------------------------------------------
# Optional submitter allowlist
# ---------------------------------------------------------------------------


def test_verification_is_open_by_default(direct_vm, verifier, at_time, direct_bob):
    """Permissionless is the default because consensus, not the submitter, is
    what makes an answer trustworthy."""
    at_time(NOW)
    assert verifier.get_config()["submitters_restricted"] is False

    direct_vm.sender = direct_bob
    mock_all_agree(direct_vm)
    assert verifier.verify_transaction(TEST_CHAIN, TX_HASH)["confirmed"] is True


def test_restricting_submitters_blocks_strangers(direct_vm, verifier, at_time, direct_bob):
    """An operator paying for a shared deployment may want to cap who can
    trigger work on it. This is a cost control, not a trust control."""
    at_time(NOW)
    verifier.set_submitters_restricted(True)

    direct_vm.sender = direct_bob
    mock_all_agree(direct_vm)
    with pytest.raises(Exception, match=r"\[EXPECTED\] submitter not allowed"):
        verifier.verify_transaction(TEST_CHAIN, TX_HASH)


def test_an_allowlisted_submitter_may_verify(direct_vm, verifier, at_time, direct_bob):
    at_time(NOW)
    verifier.set_submitters_restricted(True)
    verifier.set_submitter_allowed(direct_bob, True)

    direct_vm.sender = direct_bob
    mock_all_agree(direct_vm)
    assert verifier.verify_transaction(TEST_CHAIN, TX_HASH)["confirmed"] is True


def test_an_allowlisted_submitter_can_be_revoked(direct_vm, verifier, at_time, direct_bob):
    at_time(NOW)
    verifier.set_submitters_restricted(True)
    verifier.set_submitter_allowed(direct_bob, True)
    verifier.set_submitter_allowed(direct_bob, False)

    direct_vm.sender = direct_bob
    mock_all_agree(direct_vm)
    with pytest.raises(Exception, match=r"\[EXPECTED\] submitter not allowed"):
        verifier.verify_transaction(TEST_CHAIN, TX_HASH)


def test_the_owner_is_always_an_allowed_submitter(direct_vm, verifier, at_time, direct_owner):
    """So enabling the restriction cannot lock the operator out of their own
    deployment."""
    at_time(NOW)
    verifier.set_submitters_restricted(True)

    direct_vm.sender = direct_owner
    mock_all_agree(direct_vm)
    assert verifier.verify_transaction(TEST_CHAIN, TX_HASH)["confirmed"] is True


def test_restriction_applies_to_every_verification_mode(
    direct_vm, verifier, at_time, direct_bob
):
    at_time(NOW)
    verifier.set_submitters_restricted(True)
    direct_vm.sender = direct_bob
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm)

    for call in (
        lambda: verifier.verify_transaction(TEST_CHAIN, TX_HASH),
        lambda: verifier.refresh_transaction(TEST_CHAIN, TX_HASH),
        lambda: verifier.verify_claim(TEST_CHAIN, TX_HASH, "something"),
        lambda: verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT})),
    ):
        with pytest.raises(Exception, match=r"\[EXPECTED\] submitter not allowed"):
            call()


def test_reads_stay_open_under_restriction(direct_vm, verifier, at_time, direct_owner, direct_bob):
    """The allowlist governs who may trigger work, never who may read the
    result. A consumer contract is a reader and must not be affected."""
    at_time(NOW)
    direct_vm.sender = direct_owner
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    verifier.set_submitters_restricted(True)

    direct_vm.sender = direct_bob
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True
    assert verifier.get_verification(TEST_CHAIN, TX_HASH)["confirmed"] is True


def test_lifting_the_restriction_restores_open_access(
    direct_vm, verifier, at_time, direct_owner, direct_bob
):
    at_time(NOW)
    direct_vm.sender = direct_owner
    verifier.set_submitters_restricted(True)
    verifier.set_submitters_restricted(False)

    direct_vm.sender = direct_bob
    mock_all_agree(direct_vm)
    assert verifier.verify_transaction(TEST_CHAIN, TX_HASH)["confirmed"] is True


# ---------------------------------------------------------------------------
# Pure helper views are always open
# ---------------------------------------------------------------------------


def test_helper_views_work_for_anyone(direct_vm, verifier, direct_bob):
    """These are offline helpers for building a correct assertion. Gating them
    would only make integration harder without protecting anything."""
    direct_vm.sender = direct_bob
    assert verifier.compute_topic0("Transfer(address,address,uint256)").startswith("0x")
    assert verifier.normalize_assertion(json.dumps({"address": USDT}))
    assert verifier.known_event_signatures()["count"] > 0
    assert verifier.decode_logs("[]") == {"decoded": []}


def test_helper_views_work_while_paused(verifier):
    verifier.set_paused(True)
    assert verifier.compute_topic0("Transfer(address,address,uint256)").startswith("0x")
    assert verifier.known_event_signatures()["count"] > 0
