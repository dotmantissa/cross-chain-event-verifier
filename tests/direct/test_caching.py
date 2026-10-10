"""Caching, expiry and reorg safety.

The cache is what makes ``is_verified`` cheap enough for other contracts to
call, but a cached answer that outlives its truth is worse than no cache. So
record lifetime is a function of finality rather than a single constant:

  final       long lived, because a finalised transaction cannot change
  confirming  short lived, because it can still be reorganised out
  absent      shortest, because absence is the most perishable answer of all:
              a transaction that is missing now may be mined a second later

Expiry is evaluated against the deterministic clock, so every validator agrees
on whether a record is still usable.
"""

import json

import pytest

from conftest import (
    ALPHA,
    BETA,
    GAMMA,
    TEST_CHAIN,
    TX_HASH,
    USDT,
    blockscout_logs,
    blockscout_transaction,
    evm_batch,
    mock_all_agree,
    mock_blockscout,
    mock_llm_verdict,
    mock_rpc,
    rpc_receipt,
    rpc_transaction,
)

BLOCK = 16777216
NOW = "2026-01-15T10:30:00Z"
NOW_EPOCH = 1768473000

TTL_FINAL = 30 * 24 * 60 * 60
TTL_CONFIRMING = 10 * 60
TTL_ABSENT = 3 * 60


def mock_absent(direct_vm) -> None:
    empty = evm_batch(None, None, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, empty)
    mock_rpc(direct_vm, BETA, empty)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))


# ---------------------------------------------------------------------------
# Lifetime is a function of finality
# ---------------------------------------------------------------------------


def test_a_finalised_record_is_long_lived(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["expires_at"] == NOW_EPOCH + TTL_FINAL


def test_a_confirming_record_expires_quickly(direct_vm, verifier, at_time):
    """Still reorganisable, so it must be re-checked soon."""
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["confirmed"] is False
    assert out["expires_at"] == NOW_EPOCH + TTL_CONFIRMING


def test_an_absent_record_expires_soonest(direct_vm, verifier, at_time):
    """A transaction that is missing now may be mined a second later, so this
    answer has the shortest life of all."""
    at_time(NOW)
    mock_absent(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["expires_at"] == NOW_EPOCH + TTL_ABSENT


def test_a_near_threshold_record_does_not_get_the_long_ttl(direct_vm, verifier, at_time):
    """Confirmed, but only just. Treating a borderline reading as permanent
    would bake in the one case most likely to move."""
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 13)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["confirmed"] is True
    assert out["near_threshold"] is True
    assert out["expires_at"] == NOW_EPOCH + TTL_CONFIRMING


def test_a_disputed_record_expires_soon(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(value=0), rpc_receipt(), BLOCK + 64))
    mock_rpc(direct_vm, BETA, evm_batch(rpc_transaction(value=5), rpc_receipt(), BLOCK + 64))
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["status"] == "disputed"
    assert out["expires_at"] == NOW_EPOCH + TTL_ABSENT


# ---------------------------------------------------------------------------
# Cache hits
# ---------------------------------------------------------------------------


def test_a_second_call_is_served_from_cache(direct_vm, verifier, at_time):
    """The mocks are cleared after the first call. A second call that still
    returns a confirmed answer proves no network request was made, since an
    unmocked request would raise."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    direct_vm.clear_mocks()
    cached = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert cached["confirmed"] is True
    assert cached["epoch"] == 1
    assert verifier.stats()["cache_hits"] == 1
    assert verifier.stats()["verifications"] == 1


def test_a_cached_claim_is_not_readjudicated(direct_vm, verifier, at_time):
    """No model is consulted on a cache hit. With the mocks cleared, a second
    call that returns a verdict proves the stored one was reused."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="match", confidence=91)
    verifier.verify_claim(TEST_CHAIN, TX_HASH, "a transfer happened")

    direct_vm.clear_mocks()
    cached = verifier.verify_claim(TEST_CHAIN, TX_HASH, "a transfer happened")
    assert cached["verdict"] == "match"
    assert cached["confidence"] == 91
    assert verifier.stats()["claims"] == 1


def test_a_cached_proof_is_not_rechecked(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    assertion = json.dumps({"address": USDT})
    verifier.prove_event(TEST_CHAIN, TX_HASH, assertion)

    direct_vm.clear_mocks()
    cached = verifier.prove_event(TEST_CHAIN, TX_HASH, assertion)
    assert cached["satisfied"] is True
    assert verifier.stats()["proofs"] == 1


def test_records_are_keyed_per_chain(direct_vm, verifier, at_time):
    """The same transaction hash on two chains is two different questions, so
    one must never serve the other's answer."""
    at_time(NOW)
    verifier.register_chain(
        "88888",
        "Other Chain",
        "evm",
        88888,
        12,
        10,
        2,
        json.dumps(
            [
                {"label": "alpha", "kind": "evm_jsonrpc", "url": ALPHA},
                {"label": "beta", "kind": "evm_jsonrpc", "url": BETA},
            ]
        ),
    )
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True
    assert verifier.is_verified("88888", TX_HASH) is False


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_an_expired_record_stops_being_verified(direct_vm, verifier, at_time):
    """The property that makes the cache safe. Once a record is past its
    expiry, is_verified answers false even though the record is still stored."""
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert verifier.record_status(TEST_CHAIN, TX_HASH) == "success"

    at_time("2026-01-15T10:41:00Z")
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False
    assert verifier.record_status(TEST_CHAIN, TX_HASH) == "expired"


def test_expiry_is_reported_on_the_record(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    at_time("2026-01-15T10:41:00Z")
    out = verifier.get_verification(TEST_CHAIN, TX_HASH)
    assert out["found"] is True
    assert out["expired"] is True
    assert out["valid"] is False
    # The underlying facts are still readable, just no longer authoritative.
    assert out["block_number"] == BLOCK


def test_expiry_is_exclusive_at_the_boundary(direct_vm, verifier, at_time):
    """A record is usable up to but not including its expiry second."""
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    at_time("2026-01-15T10:39:59Z")
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False  # not confirmed anyway

    at_time(NOW)
    direct_vm.clear_mocks()
    mock_all_agree(direct_vm)
    verifier.refresh_transaction(TEST_CHAIN, TX_HASH)

    # One second before expiry it is still valid.
    at_time("2026-02-14T10:29:59Z")
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True

    # At expiry it is not.
    at_time("2026-02-14T10:30:00Z")
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False


def test_an_expired_claim_stops_being_valid(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    mock_llm_verdict(direct_vm)
    verifier.verify_claim(TEST_CHAIN, TX_HASH, "a transfer happened")

    at_time("2026-01-15T10:41:00Z")
    assert verifier.is_claim_verified(TEST_CHAIN, TX_HASH, "a transfer happened") is False


def test_an_expired_proof_stops_being_valid(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    assertion = json.dumps({"address": USDT})
    verifier.prove_event(TEST_CHAIN, TX_HASH, assertion)

    at_time("2026-01-15T10:41:00Z")
    assert verifier.is_event_proven(TEST_CHAIN, TX_HASH, assertion) is False


def test_an_expired_record_is_re_verified_on_the_next_call(direct_vm, verifier, at_time):
    """Expiry is what lets a pending transaction become confirmed. The first
    call saw it with two confirmations, the second with plenty."""
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    first = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert first["confirmed"] is False
    assert first["epoch"] == 1

    at_time("2026-01-15T10:41:00Z")
    direct_vm.clear_mocks()
    mock_all_agree(direct_vm, head=BLOCK + 500)
    second = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert second["confirmed"] is True
    assert second["epoch"] == 2
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True


# ---------------------------------------------------------------------------
# Forced refresh
# ---------------------------------------------------------------------------


def test_refresh_bypasses_a_fresh_cache_entry(direct_vm, verifier, at_time):
    """Useful when a transaction was last seen pending or disputed and is
    expected to have settled since."""
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    direct_vm.clear_mocks()
    mock_all_agree(direct_vm, head=BLOCK + 500)
    refreshed = verifier.refresh_transaction(TEST_CHAIN, TX_HASH)
    assert refreshed["confirmed"] is True
    assert refreshed["epoch"] == 2


def test_refresh_increments_the_epoch_each_time(direct_vm, verifier, at_time):
    """The epoch is a monotonic record version, so a consumer can tell whether
    an answer changed since it last looked."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    assert verifier.verify_transaction(TEST_CHAIN, TX_HASH)["epoch"] == 1

    for expected in (2, 3):
        direct_vm.clear_mocks()
        mock_all_agree(direct_vm)
        assert verifier.refresh_transaction(TEST_CHAIN, TX_HASH)["epoch"] == expected


def test_refresh_can_reverse_a_verdict(direct_vm, verifier, at_time):
    """Honest about reorgs. If the sources now say the transaction is gone, the
    stored answer flips to absent rather than keeping the old confirmation."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True

    direct_vm.clear_mocks()
    mock_absent(direct_vm)
    reversed_record = verifier.refresh_transaction(TEST_CHAIN, TX_HASH)
    assert reversed_record["exists"] is False
    assert reversed_record["status"] == "absent"
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False


def test_a_record_is_only_counted_once_in_the_index(direct_vm, verifier, at_time):
    """Re-verifying must not append a duplicate key, or list_records and
    purge_expired would both drift."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    for _ in range(3):
        direct_vm.clear_mocks()
        mock_all_agree(direct_vm)
        verifier.refresh_transaction(TEST_CHAIN, TX_HASH)

    assert verifier.stats()["records"] == 1
    assert verifier.list_records(0, 10)["total"] == 1


# ---------------------------------------------------------------------------
# Cache policy
# ---------------------------------------------------------------------------


def test_the_owner_can_retune_lifetimes(direct_vm, verifier, at_time):
    at_time(NOW)
    verifier.set_cache_policy(3600, 60, 30)
    config = verifier.get_config()
    assert config["ttl_final"] == 3600
    assert config["ttl_confirming"] == 60
    assert config["ttl_absent"] == 30

    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["expires_at"] == NOW_EPOCH + 3600


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_lifetime_is_rejected(verifier, bad):
    """A zero lifetime would make every record instantly stale, which is a
    denial of service rather than a configuration."""
    with pytest.raises(Exception, match="must be at least 1 second"):
        verifier.set_cache_policy(bad, 60, 30)
    with pytest.raises(Exception, match="must be at least 1 second"):
        verifier.set_cache_policy(3600, bad, 30)
    with pytest.raises(Exception, match="must be at least 1 second"):
        verifier.set_cache_policy(3600, 60, bad)


# ---------------------------------------------------------------------------
# Purging
# ---------------------------------------------------------------------------


def test_purge_removes_only_expired_records(direct_vm, verifier, at_time):
    """A live record must survive a purge, since a consumer may still rely on
    it. Only records already past expiry are removable."""
    at_time(NOW)
    stale = "0x" + "11" * 32
    fresh = "0x" + "22" * 32

    mock_all_agree(direct_vm, head=BLOCK + 2, tx_hash=stale)
    verifier.verify_transaction(TEST_CHAIN, stale)

    direct_vm.clear_mocks()
    mock_all_agree(direct_vm, tx_hash=fresh)
    verifier.verify_transaction(TEST_CHAIN, fresh)

    at_time("2026-01-15T10:41:00Z")
    out = verifier.purge_expired(100)
    assert out["removed"] == 1
    assert out["remaining"] == 1

    assert verifier.get_verification(TEST_CHAIN, stale)["found"] is False
    assert verifier.get_verification(TEST_CHAIN, fresh)["found"] is True
    assert verifier.is_verified(TEST_CHAIN, fresh) is True


def test_purge_respects_its_budget(direct_vm, verifier, at_time):
    """Bounded so a large registry cannot exceed the compute limit in one call."""
    at_time(NOW)
    hashes = ["0x" + f"{i:02x}" * 32 for i in range(1, 6)]
    for tx in hashes:
        direct_vm.clear_mocks()
        mock_all_agree(direct_vm, head=BLOCK + 2, tx_hash=tx)
        verifier.verify_transaction(TEST_CHAIN, tx)

    at_time("2026-01-15T10:41:00Z")
    first = verifier.purge_expired(2)
    assert first["removed"] == 2
    assert first["remaining"] == 3

    second = verifier.purge_expired(100)
    assert second["removed"] == 3
    assert second["remaining"] == 0


def test_purge_on_an_empty_registry_is_a_no_op(verifier, at_time):
    at_time(NOW)
    out = verifier.purge_expired(10)
    assert out["removed"] == 0
    assert out["remaining"] == 0


def test_purge_keeps_the_index_consistent(direct_vm, verifier, at_time):
    """After a purge, listing has to reflect what is actually stored."""
    at_time(NOW)
    hashes = ["0x" + f"{i:02x}" * 32 for i in range(1, 4)]
    for index, tx in enumerate(hashes):
        direct_vm.clear_mocks()
        head = BLOCK + 2 if index < 2 else BLOCK + 500
        mock_all_agree(direct_vm, head=head, tx_hash=tx)
        verifier.verify_transaction(TEST_CHAIN, tx)

    at_time("2026-01-15T10:41:00Z")
    verifier.purge_expired(100)

    listing = verifier.list_records(0, 10)
    assert listing["total"] == 1
    assert listing["records"][0]["tx_hash"] == hashes[2]
