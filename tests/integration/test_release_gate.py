"""Actual finalized cross-contract settlement and replay protection."""
import base64
import binascii
import json
import os
import uuid
import pytest
from genlayer_py.types import CalldataAddress, TransactionStatus
from gltest.assertions import tx_execution_succeeded
from conftest import ETH, TX, ABSENT_TX, LONG_WAIT, transfer_assertion


def transact(call):
    receipt = call.transact(wait_transaction_status=TransactionStatus.FINALIZED, **LONG_WAIT)
    assert tx_execution_succeeded(receipt), receipt
    return receipt


def rejected(call, reason):
    receipt = call.transact(**LONG_WAIT)
    assert not tx_execution_succeeded(receipt), receipt
    assert receipt.get("consensus_data", {}).get("leader_receipt"), receipt
    leader = receipt["consensus_data"]["leader_receipt"][0]
    assert leader.get("execution_result") != "SUCCESS", receipt
    text = str(receipt)
    raw = leader.get("result", "")
    if isinstance(raw, str):
        try:
            text += base64.b64decode(raw, validate=True).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            pass
    assert reason in text, receipt


@pytest.fixture
def condition_id():
    return "integration-" + uuid.uuid4().hex[:16]


def create(gate, identifier, assertion=""):
    beneficiary = gate.get_config().call()["owner"]
    return transact(gate.create_condition(args=[identifier, ETH, assertion,
        "Live integration condition", CalldataAddress(beneficiary), 0]))


def test_gate_configuration_matches_requested_read_policy(release_gate):
    config = release_gate.get_config().call()
    assert config["require_strict"] is True
    assert config["allow_non_final"] is (os.getenv("CCEV_GATE_NONFINAL_DIAGNOSTIC") == "1")


def test_event_condition_settles_and_blocks_replay(release_gate, proven_fixture_event, condition_id):
    second = condition_id + "-replay"
    previous = release_gate.settled_condition_for(args=[ETH, TX]).call()
    if previous:
        # Checkpoint reuse preserves the consumer's permanent replay history.
        condition_id = previous
    else:
        create(release_gate, condition_id, proven_fixture_event)
        record = release_gate.get_condition(args=[condition_id]).call()
        assert record["assertion"]
        assert record["open"] is True
        assert release_gate.check_readiness(args=[condition_id, TX]).call()["ready"] is True
        transact(release_gate.settle(args=[condition_id, TX]))
    settled = release_gate.get_condition(args=[condition_id]).call()
    assert settled["settled"] is True
    assert settled["settled_tx"] == TX
    assert release_gate.is_settled(args=[condition_id]).call() is True
    assert release_gate.settled_condition_for(args=[ETH, TX.upper()]).call() == condition_id
    rejected(release_gate.settle(args=[condition_id, TX]), "already settled")
    create(release_gate, second, proven_fixture_event)
    assert release_gate.check_readiness(args=[second, TX.upper()]).call()["ready"] is False
    rejected(release_gate.settle(args=[second, TX.upper()]), "transaction already settled")
    assert release_gate.is_settled(args=[second]).call() is False


def test_an_unverified_transaction_cannot_settle(release_gate, condition_id):
    create(release_gate, condition_id)
    assert release_gate.check_readiness(args=[condition_id, ABSENT_TX]).call()["ready"] is False
    rejected(release_gate.settle(args=[condition_id, ABSENT_TX]), "has not confirmed")
    assert release_gate.is_settled(args=[condition_id]).call() is False


def test_cancelled_conditions_cannot_settle(release_gate, condition_id):
    create(release_gate, condition_id, transfer_assertion())
    transact(release_gate.cancel_condition(args=[condition_id]))
    assert release_gate.get_condition(args=[condition_id]).call()["cancelled"] is True
    rejected(release_gate.settle(args=[condition_id, TX]), "condition is cancelled")


def test_unknown_condition_read_is_safe(release_gate):
    assert release_gate.is_settled(args=["does-not-exist"]).call() is False
    assert release_gate.get_condition(args=["does-not-exist"]).call()["found"] is False


def test_strict_policy_applies_to_event_conditions(release_gate, verifier, condition_id):
    """A real proof can be satisfied yet sit inside the confirmation band."""
    chain = "band-" + uuid.uuid4().hex[:12]
    ethereum = verifier.get_chain(args=[ETH]).call()
    sources = [{"label": source["label"], "kind": source["kind"], "url": source["url"]}
        for source in ethereum["sources"]]
    transact(verifier.register_chain(args=[chain, "Threshold regression", "evm", 1,
        1, 1000000000, 2, json.dumps(sources)]))
    assertion = transfer_assertion()
    transact(verifier.prove_event(args=[chain, TX, assertion]))
    assert verifier.is_event_proven(args=[chain, TX, assertion]).call() is True
    assert verifier.is_verified_strict(args=[chain, TX]).call() is False
    beneficiary = release_gate.get_config().call()["owner"]
    transact(release_gate.create_condition(args=[condition_id, chain, assertion,
        "Strict confirmation regression", CalldataAddress(beneficiary), 0]))
    assert release_gate.check_readiness(args=[condition_id, TX]).call()["ready"] is False
    rejected(release_gate.settle(args=[condition_id, TX]), "not strictly confirmed")
    try:
        transact(release_gate.set_require_strict(args=[False]))
        assert release_gate.check_readiness(args=[condition_id, TX]).call()["ready"] is True
        transact(release_gate.settle(args=[condition_id, TX]))
        assert release_gate.is_settled(args=[condition_id]).call() is True
    finally:
        transact(release_gate.set_require_strict(args=[True]))
        transact(verifier.set_chain_enabled(args=[chain, False]))
