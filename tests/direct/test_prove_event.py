"""Structured event proofs, the deterministic verification mode.

No language model is involved at any point in this path. An assertion is
matched against the receipt's logs in code, and the result holds only when the
transaction is also confirmed. This is the strongest of the three modes and the
one to reach for when the event is known precisely.

None of the tests here register an LLM mock. If the contract tried to call a
model on this path, every one of them would fail with a missing-mock error,
which is the point: the absence of those mocks is itself the assertion that
this mode is model-free.
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
    USDT,
    blockscout_logs,
    blockscout_transaction,
    erc20_transfer_log,
    erc721_transfer_log,
    evm_batch,
    mock_all_agree,
    mock_blockscout,
    mock_rpc,
    rpc_receipt,
    rpc_transaction,
    topic_pad,
    word,
)

BLOCK = 16777216
NOW = "2026-01-15T10:30:00Z"

TRANSFER_SIG = "Transfer(address,address,uint256)"


def mock_logs(direct_vm, logs, head: int = BLOCK + 64) -> None:
    body = evm_batch(rpc_transaction(), rpc_receipt(logs=logs), head)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(confirmations=head - BLOCK + 1),
        blockscout_logs(logs),
    )


# ---------------------------------------------------------------------------
# Proving an event
# ---------------------------------------------------------------------------


def test_a_matching_event_is_proven(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)

    out = verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": USDT, "signature": TRANSFER_SIG})
    )
    assert out["satisfied"] is True
    assert out["tx_confirmed"] is True
    assert out["valid"] is True
    assert out["matched_count"] == 1
    assert out["matched_indices"] == [0]


def test_is_event_proven_answers_true(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    assertion = json.dumps({"address": USDT, "signature": TRANSFER_SIG})
    verifier.prove_event(TEST_CHAIN, TX_HASH, assertion)

    assert verifier.is_event_proven(TEST_CHAIN, TX_HASH, assertion) is True


def test_a_non_matching_event_is_not_proven(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)

    out = verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": "0x" + "99" * 20})
    )
    assert out["satisfied"] is False
    assert out["valid"] is False
    assert out["matched_count"] == 0
    assert out["note"] == "no matching log"


def test_the_transaction_record_comes_back_alongside_the_proof(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)

    out = verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT}))
    assert out["transaction"]["confirmed"] is True
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True


def test_an_unproven_assertion_reads_as_absent(verifier):
    out = verifier.get_event_proof(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT}))
    assert out["found"] is False
    assert out["satisfied"] is False
    assert out["valid"] is False


# ---------------------------------------------------------------------------
# Precision: proving exactly the event that was asked about
# ---------------------------------------------------------------------------


def test_an_exact_recipient_and_amount_can_be_proven(direct_vm, verifier, at_time):
    """The shape a real escrow needs: not merely that a transfer happened, but
    that this recipient received this exact amount."""
    at_time(NOW)
    mock_all_agree(direct_vm)

    out = verifier.prove_event(
        TEST_CHAIN,
        TX_HASH,
        json.dumps(
            {
                "address": USDT,
                "signature": TRANSFER_SIG,
                "topics": {"2": RECIPIENT},
                "data_contains": "0x" + word(100000000),
            }
        ),
    )
    assert out["satisfied"] is True


def test_the_wrong_amount_fails_even_with_the_right_recipient(direct_vm, verifier, at_time):
    """The attack this blocks: a transfer to the right place for the wrong
    amount. All criteria have to hold on a single log."""
    at_time(NOW)
    mock_all_agree(direct_vm)

    out = verifier.prove_event(
        TEST_CHAIN,
        TX_HASH,
        json.dumps(
            {
                "address": USDT,
                "topics": {"2": RECIPIENT},
                "data_contains": "0x" + word(999),
            }
        ),
    )
    assert out["satisfied"] is False


def test_the_wrong_recipient_fails_even_with_the_right_amount(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)

    out = verifier.prove_event(
        TEST_CHAIN,
        TX_HASH,
        json.dumps(
            {
                "address": USDT,
                "topics": {"2": "0x" + "99" * 20},
                "data_contains": "0x" + word(100000000),
            }
        ),
    )
    assert out["satisfied"] is False


def test_an_nft_transfer_can_be_proven_by_token_id(direct_vm, verifier, at_time):
    """One of the motivating cases: release funds once a specific NFT moved."""
    at_time(NOW)
    collection = "0xb47e3cd837ddf8e4c57f05d70ab865de6e193bbb"
    mock_logs(direct_vm, [erc721_transfer_log(collection=collection, token_id=7804)])

    out = verifier.prove_event(
        TEST_CHAIN,
        TX_HASH,
        json.dumps(
            {
                "address": collection,
                "signature": TRANSFER_SIG,
                "topics": {"2": RECIPIENT, "3": "0x" + word(7804)},
            }
        ),
    )
    assert out["satisfied"] is True


def test_a_different_token_id_does_not_satisfy(direct_vm, verifier, at_time):
    at_time(NOW)
    collection = "0xb47e3cd837ddf8e4c57f05d70ab865de6e193bbb"
    mock_logs(direct_vm, [erc721_transfer_log(collection=collection, token_id=7804)])

    out = verifier.prove_event(
        TEST_CHAIN,
        TX_HASH,
        json.dumps(
            {
                "address": collection,
                "signature": TRANSFER_SIG,
                "topics": {"3": "0x" + word(1)},
            }
        ),
    )
    assert out["satisfied"] is False


def test_min_count_requires_several_matching_logs(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_logs(
        direct_vm,
        [erc20_transfer_log(amount=1), erc20_transfer_log(amount=2), erc20_transfer_log(amount=3)],
    )

    enough = verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": USDT, "min_count": 3})
    )
    assert enough["satisfied"] is True
    assert enough["matched_count"] == 3

    too_many_required = verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": USDT, "min_count": 4})
    )
    assert too_many_required["satisfied"] is False


def test_matching_runs_over_every_log_not_only_the_stored_summary(
    direct_vm, verifier, mod, at_time
):
    """Only a bounded number of decoded events are persisted, but assertion
    matching happens inside the consensus block against the full receipt. A
    log beyond the storage cap is still provable, so the cap never weakens the
    guarantee."""
    at_time(NOW)
    cap = mod.MAX_DECODED_EVENTS_STORED
    filler = [
        {"address": "0x" + "11" * 20, "topics": ["0x" + "ee" * 32], "data": "0x"}
        for _ in range(cap + 5)
    ]
    needle = erc20_transfer_log(amount=4242)
    mock_logs(direct_vm, filler + [needle])

    out = verifier.prove_event(
        TEST_CHAIN,
        TX_HASH,
        json.dumps({"address": USDT, "data_contains": "0x" + word(4242)}),
    )
    assert out["satisfied"] is True
    assert out["matched_indices"] == [cap + 5]
    # The stored decoded summary is capped, yet the proof still held.
    assert len(out["transaction"]["decoded_events"]) == cap


# ---------------------------------------------------------------------------
# A proof requires a confirmed transaction
# ---------------------------------------------------------------------------


def test_matching_logs_on_an_unconfirmed_transaction_do_not_prove(
    direct_vm, verifier, at_time
):
    """The log is there, but the transaction has not reached the chain's
    confirmation threshold. Acting on it would be acting on a reorg risk."""
    at_time(NOW)
    mock_all_agree(direct_vm, head=BLOCK + 2)

    out = verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": USDT, "signature": TRANSFER_SIG})
    )
    assert out["satisfied"] is False
    assert out["tx_confirmed"] is False
    assert out["matched_count"] == 1
    assert out["note"] == "logs matched but the transaction is not confirmed"


def test_logs_from_a_reverted_transaction_do_not_prove(direct_vm, verifier, at_time):
    """A reverted transaction's logs are discarded by the chain, but even if a
    source reported them, a revert is never confirmed."""
    at_time(NOW)
    body = evm_batch(rpc_transaction(), rpc_receipt(status=0), BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(confirmations=65, result="Reverted", status="error"),
        blockscout_logs(),
    )

    out = verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT}))
    assert out["satisfied"] is False
    assert out["tx_confirmed"] is False


def test_an_absent_transaction_proves_nothing(direct_vm, verifier, at_time):
    at_time(NOW)
    empty = evm_batch(None, None, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, empty)
    mock_rpc(direct_vm, BETA, empty)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))

    out = verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT}))
    assert out["satisfied"] is False
    assert out["note"] == "transaction is not confirmed"


def test_a_single_corroborating_source_does_not_prove(direct_vm, verifier, at_time):
    """Quorum applies to the deterministic path exactly as it does elsewhere."""
    at_time(NOW)
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64))
    mock_rpc(direct_vm, BETA, "{}", status=503)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, "{}", status=503)

    out = verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT}))
    assert out["satisfied"] is False
    assert out["tx_confirmed"] is False


# ---------------------------------------------------------------------------
# Assertion keying
# ---------------------------------------------------------------------------


def test_assertions_are_keyed_by_canonical_form(direct_vm, verifier, at_time):
    """Two spellings of the same assertion resolve to the same record, so a
    consumer reading with different key order still finds the proof."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": USDT, "signature": TRANSFER_SIG})
    )

    reordered = json.dumps({"signature": TRANSFER_SIG, "address": USDT})
    assert verifier.is_event_proven(TEST_CHAIN, TX_HASH, reordered) is True


def test_address_casing_does_not_split_the_record(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT}))

    upper = json.dumps({"address": USDT.upper().replace("0X", "0x")})
    assert verifier.is_event_proven(TEST_CHAIN, TX_HASH, upper) is True


def test_different_assertions_are_separate_records(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    good = json.dumps({"address": USDT})
    bad = json.dumps({"address": "0x" + "99" * 20})
    verifier.prove_event(TEST_CHAIN, TX_HASH, good)
    verifier.prove_event(TEST_CHAIN, TX_HASH, bad)

    assert verifier.is_event_proven(TEST_CHAIN, TX_HASH, good) is True
    assert verifier.is_event_proven(TEST_CHAIN, TX_HASH, bad) is False


def test_a_malformed_assertion_is_rejected_before_consensus(verifier):
    """Validation happens up front, so a bad assertion costs nothing."""
    with pytest.raises(Exception, match="at least one of"):
        verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({}))


def test_reading_with_a_malformed_assertion_also_raises(verifier):
    with pytest.raises(Exception, match="not valid JSON"):
        verifier.is_event_proven(TEST_CHAIN, TX_HASH, "{broken")


def test_proof_statistics_are_counted(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    verifier.prove_event(TEST_CHAIN, TX_HASH, json.dumps({"address": USDT}))

    assert verifier.stats()["proofs"] == 1


def test_compute_topic0_helps_build_an_assertion(verifier):
    """A builder can derive the topic0 for any signature without hardcoding
    hashes, then assert on it directly."""
    topic0 = verifier.compute_topic0(TRANSFER_SIG)
    canonical = verifier.normalize_assertion(json.dumps({"topic0": topic0}))
    assert topic0 in canonical
