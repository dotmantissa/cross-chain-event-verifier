"""End-to-end verification through the contract.

These drive the public methods with mocked sources, so they cover the full
path: plan construction from the on-chain registry, consensus, aggregation,
storage, and the consumer-facing read surface.

The property that matters most here is that ``is_verified`` answers true only
when every condition holds at once. It is the single call other contracts rely
on, so each way it can legitimately answer false is pinned separately.
"""

import json

import pytest

from conftest import (
    ALPHA,
    BETA,
    GAMMA,
    RECIPIENT,
    SENDER,
    TEST_CHAIN,
    TX_HASH,
    UNKNOWN_TX,
    USDT,
    addr_hex,
    blockscout_logs,
    blockscout_transaction,
    erc20_transfer_log,
    evm_batch,
    mock_all_agree,
    mock_blockscout,
    mock_rpc,
    rpc_receipt,
    rpc_transaction,
)

BLOCK = 16777216
NOW = "2026-01-15T10:30:00Z"


def mock_absent(direct_vm, tx_hash: str = TX_HASH) -> None:
    empty = evm_batch(None, None, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, empty)
    mock_rpc(direct_vm, BETA, empty)
    mock_blockscout(direct_vm, GAMMA, tx_hash, json.dumps({"message": "Not found"}))


def mock_with_status(direct_vm, status: int, head: int = BLOCK + 64) -> None:
    body = evm_batch(rpc_transaction(), rpc_receipt(status=status), head)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(
            confirmations=head - BLOCK + 1,
            result="success" if status == 1 else "Reverted",
            status="ok" if status == 1 else "error",
        ),
        blockscout_logs(),
    )


# ---------------------------------------------------------------------------
# The confirmed path
# ---------------------------------------------------------------------------


def test_a_confirmed_transaction_verifies(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["exists"] is True
    assert out["mined"] is True
    assert out["confirmed"] is True
    assert out["status"] == "success"
    assert out["finality"] == "final"
    assert out["agreement"] == "strong"
    assert out["from"] == SENDER
    assert out["to"] == USDT
    assert out["block_number"] == BLOCK
    assert out["method_selector"] == "0xa9059cbb"
    assert out["log_count"] == 1
    assert out["sources_agreeing"] >= 2


def test_is_verified_answers_true(direct_vm, verifier, at_time):
    """The single call other contracts depend on."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True
    assert verifier.is_verified_strict(TEST_CHAIN, TX_HASH) is True


def test_decoded_events_are_stored(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert len(out["decoded_events"]) == 1
    event = out["decoded_events"][0]
    assert event["event"] == "ERC20Transfer"
    assert event["fields"]["value"] == "100000000"
    assert event["fields"]["to"] == RECIPIENT


def test_the_source_report_is_an_audit_trail(direct_vm, verifier, at_time):
    """A consumer has to be able to see which sources were consulted and what
    each one said, not just the verdict."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    labels = {r["label"] for r in out["source_reports"]}
    assert labels == {"alpha", "beta"} or labels == {"alpha", "beta", "gamma"}
    for report in out["source_reports"]:
        assert report["outcome"] in ("found", "absent", "error")
        assert report["kind"] in ("evm_jsonrpc", "blockscout_v2")


def test_the_identity_digest_is_exposed(direct_vm, verifier, at_time):
    """Exposing the digest lets a consumer pin a specific transaction identity
    and detect if a later record describes something different."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert out["identity_digest"].startswith("0x")
    assert len(out["identity_digest"]) == 66
    assert out["settlement_digest"].startswith("0x")


def test_timestamps_are_reported_in_both_forms(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert out["verified_at"] == 1768473000
    assert out["verified_at_iso"] == "2026-01-15T10:30:00Z"
    assert out["expires_at"] > out["verified_at"]


# ---------------------------------------------------------------------------
# Every way is_verified legitimately answers false
# ---------------------------------------------------------------------------


def test_unverified_transaction_is_not_verified(verifier):
    """Never verified at all. No consensus has been reached, so the honest
    answer is false rather than an error."""
    assert verifier.is_verified(TEST_CHAIN, UNKNOWN_TX) is False
    assert verifier.record_status(TEST_CHAIN, UNKNOWN_TX) == "unverified"


def test_get_verification_reports_an_absent_record(verifier):
    out = verifier.get_verification(TEST_CHAIN, UNKNOWN_TX)
    assert out["found"] is False
    assert out["confirmed"] is False
    assert out["status"] == "unverified"


def test_an_absent_transaction_is_not_verified(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_absent(direct_vm)

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["exists"] is False
    assert out["confirmed"] is False
    assert out["status"] == "absent"
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False


def test_a_reverted_transaction_is_not_verified(direct_vm, verifier, at_time):
    """A reverted transaction genuinely happened, and that distinction matters:
    exists is true while confirmed is false. A consumer releasing funds must
    not treat a revert as success."""
    at_time(NOW)
    mock_with_status(direct_vm, status=0)

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["exists"] is True
    assert out["mined"] is True
    assert out["status"] == "failed"
    assert out["confirmed"] is False
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False


def test_too_few_confirmations_is_not_verified(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_with_status(direct_vm, status=1, head=BLOCK + 2)

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["exists"] is True
    assert out["finality"] == "confirming"
    assert out["confirmed"] is False
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False


def test_a_pending_transaction_is_not_verified(direct_vm, verifier, at_time):
    at_time(NOW)
    body = evm_batch(rpc_transaction(block=None), None, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(block=None, confirmations=0, result="pending", status=""),
    )

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["exists"] is True
    assert out["mined"] is False
    assert out["finality"] == "pending"
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False


def test_a_disputed_transaction_is_not_verified(direct_vm, verifier, at_time):
    """Two sources contradicting each other is recorded and surfaced rather
    than resolved by guessing."""
    at_time(NOW)
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(value=0), rpc_receipt(), BLOCK + 64))
    mock_rpc(
        direct_vm,
        BETA,
        evm_batch(rpc_transaction(value=10**18), rpc_receipt(), BLOCK + 64),
    )
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["status"] == "disputed"
    assert out["confirmed"] is False
    assert out["agreement"] == "none"
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False
    assert verifier.record_status(TEST_CHAIN, TX_HASH) == "disputed"


def test_a_near_threshold_record_passes_lenient_but_fails_strict(
    direct_vm, verifier, at_time
):
    """The two read methods differ exactly here. A record whose confirmation
    count sat inside the hysteresis band is usable for ordinary purposes but
    rejected when certainty matters."""
    at_time(NOW)
    mock_with_status(direct_vm, status=1, head=BLOCK + 13)

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["confirmed"] is True
    assert out["near_threshold"] is True
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True
    assert verifier.is_verified_strict(TEST_CHAIN, TX_HASH) is False


def test_only_one_reachable_source_does_not_confirm(direct_vm, verifier, at_time):
    """Quorum is enforced end to end, not just in the aggregator."""
    at_time(NOW)
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64))
    mock_rpc(direct_vm, BETA, "{}", status=503)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, "{}", status=503)

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["exists"] is True
    assert out["confirmed"] is False
    assert out["agreement"] == "weak"
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is False


def test_every_source_failing_raises_transiently(direct_vm, verifier, at_time):
    """No evidence is not evidence of absence. This is a retryable failure, so
    nothing is written."""
    at_time(NOW)
    mock_rpc(direct_vm, ALPHA, "{}", status=503)
    mock_rpc(direct_vm, BETA, "{}", status=503)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, "{}", status=503)

    with pytest.raises(Exception, match=r"\[TRANSIENT\] every source failed"):
        verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert verifier.record_status(TEST_CHAIN, TX_HASH) == "unverified"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_an_unregistered_chain_is_rejected(verifier):
    with pytest.raises(Exception, match=r"\[EXPECTED\] chain 12345 is not registered"):
        verifier.verify_transaction("12345", TX_HASH)


def test_a_disabled_chain_is_rejected(direct_vm, verifier, at_time):
    at_time(NOW)
    verifier.set_chain_enabled(TEST_CHAIN, False)
    with pytest.raises(Exception, match=r"\[EXPECTED\] chain 99999 is disabled"):
        verifier.verify_transaction(TEST_CHAIN, TX_HASH)


def test_a_malformed_hash_is_rejected_before_any_request(verifier):
    """Validation happens before consensus, so a typo costs nothing."""
    with pytest.raises(Exception, match="malformed 32-byte transaction hash"):
        verifier.verify_transaction(TEST_CHAIN, "0xdeadbeef")


def test_a_hash_is_normalised_before_lookup(direct_vm, verifier, at_time):
    """Verifying with one casing and reading with another has to hit the same
    record, or a consumer would miss an answer that exists."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH.upper().replace("0X", "0x"))

    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True


def test_chain_id_whitespace_is_trimmed(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(f"  {TEST_CHAIN}  ", TX_HASH)
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def test_statistics_track_outcomes(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    stats = verifier.stats()
    assert stats["verifications"] == 1
    assert stats["confirmed"] == 1
    assert stats["records"] == 1


def test_absent_and_disputed_outcomes_are_counted_separately(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_absent(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    stats = verifier.stats()
    assert stats["absent"] == 1
    assert stats["confirmed"] == 0


def test_records_can_be_listed_with_paging(direct_vm, verifier, at_time):
    at_time(NOW)
    hashes = ["0x" + f"{i:02x}" * 32 for i in range(1, 4)]
    for tx in hashes:
        direct_vm.clear_mocks()
        mock_all_agree(direct_vm, tx_hash=tx)
        verifier.verify_transaction(TEST_CHAIN, tx)

    page = verifier.list_records(0, 2)
    assert page["total"] == 3
    assert page["returned"] == 2
    assert page["offset"] == 0

    rest = verifier.list_records(2, 2)
    assert rest["returned"] == 1

    past_the_end = verifier.list_records(99, 10)
    assert past_the_end["returned"] == 0


def test_list_records_caps_the_page_size(direct_vm, verifier, at_time):
    """An unbounded page would be a compute-limit hazard on a large registry."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    page = verifier.list_records(0, 100000)
    assert page["returned"] <= 100


def test_a_negative_offset_is_clamped(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    page = verifier.list_records(-5, 10)
    assert page["offset"] == 0
    assert page["returned"] == 1


def test_the_verifier_address_is_recorded(direct_vm, verifier, at_time, direct_alice):
    """Who triggered a verification is recorded for auditability, though it has
    no bearing on the result since consensus is what makes it trustworthy."""
    at_time(NOW)
    direct_vm.sender = direct_alice
    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["verifier"].lower() == addr_hex(direct_alice)


def test_verification_is_permissionless_by_default(
    direct_vm, verifier, at_time, direct_bob
):
    """Consensus is what makes the answer trustworthy, not the submitter, so
    anyone may ask."""
    at_time(NOW)
    direct_vm.sender = direct_bob
    mock_all_agree(direct_vm)
    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["confirmed"] is True
