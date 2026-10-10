"""Live verification against real block explorers, under real consensus.

Every test in this file performs or reads the result of an actual consensus
round in which each validator independently queried public Ethereum explorers.
Nothing is mocked. The assertions are against values read from live archive
RPC, so if the primitive reports anything different, it is wrong.

This is also where the design's central claim gets exercised end to end: the
validators have to agree despite reading the chain at different heights, and
despite some public endpoints being unable to serve the receipt at all.
"""

import json

import pytest
from genlayer_py.types import TransactionStatus
from gltest.assertions import tx_execution_succeeded

from conftest import (
    ABSENT_TX,
    ETH,
    LONG_WAIT,
    TRANSFER_AMOUNT,
    TRANSFER_SIG,
    TX,
    TX_BLOCK,
    TX_INDEX,
    TX_NONCE,
    TX_RECIPIENT,
    TX_SELECTOR,
    TX_SENDER,
    USDT,
    transfer_assertion,
    word,
)


# ---------------------------------------------------------------------------
# Deployment and registry
# ---------------------------------------------------------------------------


def test_the_contract_deploys_with_its_default_registry(verifier):
    config = verifier.get_config().call()
    assert config["chain_count"] >= 7
    assert config["paused"] is False

    chains = verifier.list_chains().call()
    ids = {c["chain_id"] for c in chains["chains"]}
    assert {"1", "8453", "42161", "10", "137", "11155111", "solana-mainnet"} <= ids


def test_the_ethereum_registry_entry_is_sane(verifier):
    chain = verifier.get_chain(args=[ETH]).call()
    assert chain["found"] is True
    assert chain["family"] == "evm"
    assert chain["evm_chain_id"] == 1
    assert chain["min_confirmations"] == 12
    assert chain["quorum"] == 2
    assert chain["source_count"] >= 3
    for source in chain["sources"]:
        assert source["url"].startswith("https://")
        assert source["has_api_key"] is False


# ---------------------------------------------------------------------------
# The core claim: real consensus over real explorers
# ---------------------------------------------------------------------------


def test_a_real_ethereum_transaction_reaches_consensus(verified_fixture_tx):
    """The headline test.

    Validators independently queried public Ethereum explorers, normalised
    what each returned, and agreed. That they agreed at all is the point: they
    ran at different moments, so they saw different confirmation counts, and
    some public endpoints cannot serve this receipt.
    """
    record = verified_fixture_tx
    assert record["found"] is True
    assert record["exists"] is True
    assert record["mined"] is True
    assert record["confirmed"] is True
    assert record["status"] == "success"
    assert record["finality"] == "final"
    assert record["agreement"] == "strong"


def test_the_consensus_facts_match_the_real_chain(verified_fixture_tx):
    """Correctness, not just agreement. Validators could in principle agree on
    something wrong, so each fact is checked against live archive RPC values."""
    record = verified_fixture_tx
    assert record["from"] == TX_SENDER
    assert record["to"] == USDT
    assert record["value_wei"] == "0"
    assert record["nonce"] == TX_NONCE
    assert record["block_number"] == TX_BLOCK
    assert record["tx_index"] == TX_INDEX
    assert record["method_selector"] == TX_SELECTOR
    assert record["created_contract"] == ""
    assert record["log_count"] == 1


def test_more_than_one_source_corroborated_the_result(verified_fixture_tx):
    """A confirmed verdict requires a quorum. One explorer agreeing with
    itself is not consensus."""
    record = verified_fixture_tx
    assert record["sources_agreeing"] >= 2
    assert record["sources_queried"] >= 2
    assert record["sources_positive"] >= 2
    assert record["sources_settlement_agreeing"] >= 2


def test_the_source_report_names_what_was_consulted(verified_fixture_tx):
    """The audit trail a consumer can inspect to see where the answer came
    from, including any source that failed."""
    reports = verified_fixture_tx["source_reports"]
    assert len(reports) >= 2
    for report in reports:
        assert report["label"]
        assert report["kind"] in (
            "evm_jsonrpc",
            "blockscout_v2",
            "etherscan_v2",
            "solana_jsonrpc",
        )
        assert report["outcome"] in ("found", "absent", "error")


def test_the_erc20_transfer_was_decoded_from_the_real_receipt(verified_fixture_tx):
    """Decoding runs in code, so these fields cannot have been hallucinated.
    The values are the real transfer: 100 USDT between two real addresses."""
    events = verified_fixture_tx["decoded_events"]
    assert len(events) == 1

    event = events[0]
    assert event["event"] == "ERC20Transfer"
    assert event["signature"] == TRANSFER_SIG
    assert event["address"] == USDT
    assert event["fields"]["from"] == TX_SENDER
    assert event["fields"]["to"] == TX_RECIPIENT
    assert event["fields"]["value"] == str(TRANSFER_AMOUNT)


def test_confirmation_depth_is_recorded_and_deep(verified_fixture_tx):
    """Block 16777216 is millions of blocks behind the head, so this is far
    past any plausible reorg and well clear of the hysteresis band."""
    record = verified_fixture_tx
    assert record["confirmations_at_check"] > 1000000
    assert record["chain_head_at_check"] > TX_BLOCK
    assert record["near_threshold"] is False


def test_is_verified_answers_true_on_the_live_network(verified_fixture_tx, verifier):
    """The single call other contracts depend on, answered from the real
    deployment."""
    assert verifier.is_verified(args=[ETH, TX]).call() is True
    assert verifier.is_verified_strict(args=[ETH, TX]).call() is True
    assert verifier.record_status(args=[ETH, TX]).call() == "success"


def test_a_finalised_record_gets_the_long_lifetime(verified_fixture_tx):
    """A finalised transaction cannot change, so its record is long lived
    rather than being re-verified on every read."""
    record = verified_fixture_tx
    assert record["expires_at"] - record["verified_at"] == 30 * 24 * 60 * 60
    assert record["expired"] is False
    assert record["valid"] is True


def test_a_repeat_call_is_served_from_cache(verified_fixture_tx, verifier):
    """No second consensus round. The epoch stays unchanged and the cache counter
    moves, which is what makes this primitive cheap to depend on."""
    receipt = verifier.verify_transaction(args=[ETH, TX]).transact(**LONG_WAIT)
    assert tx_execution_succeeded(receipt), receipt

    record = verifier.get_verification(args=[ETH, TX]).call()
    assert record["epoch"] == verified_fixture_tx["epoch"]
    assert verifier.stats().call()["cache_hits"] >= 1


# ---------------------------------------------------------------------------
# Deterministic event proofs over real logs
# ---------------------------------------------------------------------------


def test_the_exact_real_transfer_can_be_proven(verifier, proven_fixture_event):
    proof = verifier.get_event_proof(args=[ETH, TX, proven_fixture_event]).call()
    assert proof["found"] is True
    assert proof["satisfied"] is True
    assert proof["tx_confirmed"] is True
    assert proof["valid"] is True
    assert proof["matched_count"] == 1
    assert proof["matched_indices"] == [0]
    assert verifier.is_event_proven(args=[ETH, TX, proven_fixture_event]).call() is True


def test_the_wrong_amount_is_not_proven_against_the_real_receipt(verifier):
    """Same transaction, same parties, one digit different in the amount. This
    is the case a naive 'did a transfer happen' check would wave through."""
    wrong = transfer_assertion(amount=TRANSFER_AMOUNT + 1)
    receipt = verifier.prove_event(args=[ETH, TX, wrong]).transact(**LONG_WAIT)
    assert tx_execution_succeeded(receipt), receipt

    proof = verifier.get_event_proof(args=[ETH, TX, wrong]).call()
    assert proof["satisfied"] is False
    assert proof["valid"] is False
    assert proof["matched_count"] == 0
    assert verifier.is_event_proven(args=[ETH, TX, wrong]).call() is False


def test_the_wrong_recipient_is_not_proven_against_the_real_receipt(verifier):
    wrong = transfer_assertion(recipient="0x" + "99" * 20)
    receipt = verifier.prove_event(args=[ETH, TX, wrong]).transact(**LONG_WAIT)
    assert tx_execution_succeeded(receipt), receipt

    proof = verifier.get_event_proof(args=[ETH, TX, wrong]).call()
    assert proof["satisfied"] is False
    assert verifier.is_event_proven(args=[ETH, TX, wrong]).call() is False


# ---------------------------------------------------------------------------
# Absence, over real explorers
# ---------------------------------------------------------------------------


def test_a_transaction_that_never_existed_is_reported_absent(verifier):
    """Validators have to agree that a quorum of real explorers has no such
    transaction. Absence needs that quorum precisely because a single endpoint
    returning nothing is weak evidence."""
    receipt = verifier.verify_transaction(args=[ETH, ABSENT_TX]).transact(**LONG_WAIT)
    assert tx_execution_succeeded(receipt), receipt

    record = verifier.get_verification(args=[ETH, ABSENT_TX]).call()
    assert record["exists"] is False
    assert record["status"] == "absent"
    assert record["confirmed"] is False
    assert record["agreement"] == "strong"
    assert sum(report["outcome"] == "absent" for report in record["source_reports"]) >= 2
    assert verifier.is_verified(args=[ETH, ABSENT_TX]).call() is False


def test_an_absent_record_expires_quickly(verifier):
    """A transaction missing now may be mined a second later, so this is the
    most perishable answer the contract stores."""
    record = verifier.get_verification(args=[ETH, ABSENT_TX]).call()
    assert record["expires_at"] - record["verified_at"] == 3 * 60


# ---------------------------------------------------------------------------
# Input validation on the live deployment
# ---------------------------------------------------------------------------


def test_an_unregistered_chain_is_refused(verifier):
    from gltest.assertions import tx_execution_failed

    receipt = verifier.verify_transaction(args=["424242", TX]).transact(**LONG_WAIT)
    assert tx_execution_failed(receipt), receipt


def test_a_malformed_hash_is_refused(verifier):
    from gltest.assertions import tx_execution_failed

    receipt = verifier.verify_transaction(args=[ETH, "0xnothex"]).transact(**LONG_WAIT)
    assert tx_execution_failed(receipt), receipt


# ---------------------------------------------------------------------------
# Pure helper views on the live deployment
# ---------------------------------------------------------------------------


def test_topic0_is_computed_on_chain(verifier):
    """keccak256 runs inside the contract, so a builder never has to hardcode
    an event hash."""
    assert (
        verifier.compute_topic0(args=[TRANSFER_SIG]).call()
        == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )


def test_assertions_canonicalise_on_chain(verifier):
    """Lets a builder validate an assertion, and learn its cache key, without
    spending a transaction."""
    canonical = verifier.normalize_assertion(args=[transfer_assertion()]).call()
    parsed = json.loads(canonical)
    assert parsed["address"] == USDT
    assert parsed["topics"]["2"] == "0x" + "0" * 24 + TX_RECIPIENT[2:]
    assert parsed["min_count"] == 1


def test_the_decoder_table_is_published(verifier):
    """So a builder can see exactly which events will be decoded for them."""
    known = verifier.known_event_signatures().call()
    assert known["count"] >= 15
    assert TRANSFER_SIG in known["signatures"]


def test_logs_can_be_decoded_offline(verifier):
    """A dry run of decoding, so a builder can confirm how their event will be
    interpreted before paying for a verification."""
    out = verifier.decode_logs(
        args=[
            json.dumps(
                [
                    {
                        "address": USDT,
                        "topics": [
                            verifier.compute_topic0(args=[TRANSFER_SIG]).call(),
                            "0x" + "0" * 24 + TX_SENDER[2:],
                            "0x" + "0" * 24 + TX_RECIPIENT[2:],
                        ],
                        "data": word(TRANSFER_AMOUNT),
                    }
                ]
            )
        ]
    ).call()
    assert out["decoded"][0]["event"] == "ERC20Transfer"
    assert out["decoded"][0]["fields"]["value"] == str(TRANSFER_AMOUNT)


@pytest.mark.parametrize("claim,verdict", [
    ("This successful Ethereum transaction emitted a USDT Transfer of 100000000 base units from "
     + TX_SENDER + " to " + TX_RECIPIENT + ".", "match"),
    ("This Ethereum transaction emitted a USDT Transfer of 999999999 base units to "
     + TX_RECIPIENT + ".", "mismatch"),
])
def test_natural_language_claims_are_independently_adjudicated(verifier, claim, verdict):
    receipt = verifier.verify_claim(args=[ETH, TX, claim]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED, **LONG_WAIT)
    assert tx_execution_succeeded(receipt), receipt
    record = verifier.get_claim(args=[ETH, TX, claim]).call()
    assert record["verdict"] == verdict
    assert record["valid"] is (verdict == "match")
    assert verifier.is_claim_verified(args=[ETH, TX, claim]).call() is (verdict == "match")
