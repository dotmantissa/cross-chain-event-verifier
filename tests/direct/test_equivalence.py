"""The equivalence principle.

This is the heart of the primitive. Every verification runs under
``gl.vm.run_nondet``: the leader performs the whole pipeline, and each
validator performs the same pipeline from scratch and compares its own verdict
with the leader's. The leader's answer is never an input to the validator's
work, so the two are independent evidence rather than a format check.

Authorization, identity, settlement and supported facts match exactly.
Confirmation depth may drift within the configured limit only while every
consumer decision remains unchanged. Model prose is explanatory.
"""

import json

import pytest

from conftest import (
    ALPHA,
    BETA,
    GAMMA,
    SENDER,
    TEST_CHAIN,
    TX_HASH,
    USDT,
    blockscout_logs,
    blockscout_transaction,
    erc20_transfer_log,
    evm_batch,
    mock_all_agree,
    mock_blockscout,
    mock_llm_verdict,
    mock_rpc,
    rpc_receipt,
    rpc_transaction,
)

BLOCK = 16777216


def plan(quorum: int = 2, min_confirmations: int = 12, drift: int = 10) -> dict:
    return {
        "chain_id": TEST_CHAIN,
        "quorum": quorum,
        "min_confirmations": min_confirmations,
        "drift_tolerance": drift,
        "max_sources": 5,
        "sources": [],
        "expected_evm_chain_id": 99999,
        "family": "evm",
    }


def result(**overrides) -> dict:
    base = {
        "exists": True,
        "mined": True,
        "confirmed": True,
        "near_threshold": False,
        "status": "success",
        "finality": "final",
        "agreement": "strong",
        "identity_digest": "0x" + "aa" * 32,
        "settlement_digest": "0x" + "bb" * 32,
        "confirmations": 64,
        "from": SENDER,
        "to": USDT,
        "value": "0",
        "block_number": BLOCK,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Things that must never differ
# ---------------------------------------------------------------------------


def test_identical_results_agree(mod):
    assert mod.results_agree(result(), result(), plan()) is True


def test_existence_disagreement_fails(mod):
    """The one thing that is never negotiable."""
    assert mod.results_agree(result(), result(exists=False), plan()) is False


def test_identity_disagreement_fails(mod):
    """Identity is signed by the sender. Two honest views cannot differ, so a
    difference here means one side is reading a different transaction or a
    source is lying."""
    assert (
        mod.results_agree(result(), result(identity_digest="0x" + "cc" * 32), plan())
        is False
    )


def test_settlement_disagreement_fails_when_both_mined(mod):
    """Once both sides see the transaction mined, the receipt is immutable.
    This is what blocks an explorer misreporting a log amount."""
    assert (
        mod.results_agree(result(), result(settlement_digest="0x" + "cc" * 32), plan())
        is False
    )


def test_execution_status_disagreement_fails(mod):
    assert (
        mod.results_agree(
            result(),
            result(status="failed", settlement_digest="0x" + "cc" * 32),
            plan(),
        )
        is False
    )


def test_dispute_on_one_side_only_fails(mod):
    """Whether sources contradict each other is itself a finding both sides
    have to reach."""
    assert mod.results_agree(result(), result(status="disputed"), plan()) is False
    assert mod.results_agree(result(status="disputed"), result(), plan()) is False


def test_dispute_on_both_sides_agrees(mod):
    """Two validators both seeing their sources contradict each other have
    reached the same conclusion."""
    assert (
        mod.results_agree(
            result(status="disputed", confirmed=False),
            result(status="disputed", confirmed=False),
            plan(),
        )
        is True
    )


def test_absence_on_both_sides_agrees(mod):
    absent = result(exists=False, mined=False, confirmed=False, status="absent")
    assert mod.results_agree(absent, absent, plan()) is True


# ---------------------------------------------------------------------------
# Things that are expected to differ
# ---------------------------------------------------------------------------


def test_confirmation_depth_drift_is_bounded(mod):
    """The field that drifts most. A validator reading 40 blocks later than the
    leader sees a different number for the same transaction."""
    assert mod.results_agree(result(confirmations=64), result(confirmations=104), plan()) is False


def test_a_validator_far_ahead_disagrees(mod):
    assert (
        mod.results_agree(result(confirmations=20), result(confirmations=500000), plan())
        is False
    )


def test_gas_and_labels_are_not_compared(mod):
    """Extra keys that differ are ignored, because only the digests and the
    verdict fields are consulted."""
    assert (
        mod.results_agree(
            result(gas_used="1", explorer_label="Uniswap"),
            result(gas_used="999", explorer_label="Uniswap V2: Router"),
            plan(),
        )
        is True
    )


# ---------------------------------------------------------------------------
# The confirmation threshold boundary
# ---------------------------------------------------------------------------


def test_crossing_the_threshold_between_reads_disagrees(mod):
    """Different decisions must disagree even when honest observations advance."""
    leader = result(confirmed=False, finality="confirming", confirmations=11)
    validator = result(confirmed=True, finality="final", confirmations=13)
    assert mod.results_agree(leader, validator, plan(min_confirmations=12, drift=10)) is False


def test_a_gap_too_large_for_drift_fails(mod):
    """A leader claiming confirmed while the validator sees barely any
    confirmations is not drift, it is a disagreement about reality."""
    leader = result(confirmed=True, finality="final", confirmations=12)
    validator = result(confirmed=False, finality="confirming", confirmations=1)
    assert mod.results_agree(leader, validator, plan(min_confirmations=12, drift=10)) is False


def test_threshold_change_at_band_edge_disagrees(mod):
    """A changed authorization is rejected even at the drift boundary."""
    leader = result(confirmed=False, finality="confirming", confirmations=2)
    validator = result(confirmed=True, finality="final", confirmations=12)
    assert mod.results_agree(leader, validator, plan(min_confirmations=12, drift=10)) is False


def test_one_block_past_the_band_edge_fails(mod):
    leader = result(confirmed=False, finality="confirming", confirmations=1)
    validator = result(confirmed=True, finality="final", confirmations=12)
    assert mod.results_agree(leader, validator, plan(min_confirmations=12, drift=10)) is False


def test_zero_drift_tolerance_makes_the_verdict_exact(mod):
    """A chain configured with no tolerance requires the binary verdict to
    match outright."""
    leader = result(confirmed=False, finality="confirming", confirmations=11)
    validator = result(confirmed=True, finality="final", confirmations=12)
    assert mod.results_agree(leader, validator, plan(min_confirmations=12, drift=0)) is False


# ---------------------------------------------------------------------------
# The pending-versus-mined race
# ---------------------------------------------------------------------------


def test_mined_state_change_requires_new_consensus(mod):
    """The leader saw it pending, the validator saw it land one block later.
    Identity agrees, but the mined/settlement decision must rotate."""
    leader = result(mined=False, confirmed=False, status="pending", confirmations=0, settlement_digest="")
    validator = result(mined=True, confirmed=False, status="success", confirmations=1)
    assert mod.results_agree(leader, validator, plan(drift=10)) is False


def test_pending_against_deeply_mined_fails(mod):
    """A validator seeing 500 confirmations while the leader saw nothing mined
    cannot be explained by the time between two reads."""
    leader = result(mined=False, confirmed=False, status="pending", confirmations=0, settlement_digest="")
    validator = result(mined=True, confirmed=True, status="success", confirmations=500)
    assert mod.results_agree(leader, validator, plan(drift=10)) is False


def test_pending_state_cannot_skip_settlement_binding(mod):
    """Pending cannot bypass comparison of later-consumed settlement facts."""
    leader = result(mined=False, confirmed=False, status="pending", confirmations=0, settlement_digest="")
    validator = result(mined=True, confirmed=False, status="success", confirmations=2, settlement_digest="0x" + "ff" * 32)
    assert mod.results_agree(leader, validator, plan(drift=10)) is False


# ---------------------------------------------------------------------------
# Claim adjudication
# ---------------------------------------------------------------------------


def test_the_same_verdict_agrees(mod):
    leader = result(verdict="match", confidence=90)
    validator = result(verdict="match", confidence=85)
    assert mod.results_agree(leader, validator, plan()) is True


def test_opposite_verdicts_fail(mod):
    """The substantive decision. Two independent model runs disagreeing about
    whether a claim holds must not be averaged into an answer."""
    leader = result(verdict="match", confidence=90)
    validator = result(verdict="mismatch", confidence=90)
    assert mod.results_agree(leader, validator, plan()) is False


def test_match_against_indeterminate_fails(mod):
    leader = result(verdict="match", confidence=90)
    validator = result(verdict="indeterminate", confidence=90)
    assert mod.results_agree(leader, validator, plan()) is False


def test_confidence_within_tolerance_agrees(mod):
    """Two model runs will not produce identical confidence, so a band is
    necessary. The verdict itself still has to match exactly."""
    leader = result(verdict="match", confidence=95)
    validator = result(verdict="match", confidence=70)
    assert mod.results_agree(leader, validator, plan()) is True


def test_confidence_outside_tolerance_fails(mod):
    """A very wide confidence gap on the same verdict suggests the two runs
    were not looking at the same thing."""
    leader = result(verdict="match", confidence=100)
    validator = result(verdict="match", confidence=20)
    assert mod.results_agree(leader, validator, plan()) is False


def test_reasoning_text_is_never_compared(mod):
    """Free-form prose from a model is never reproducible word for word."""
    leader = result(verdict="match", confidence=90, reasoning="A sends 100 USDT to B.")
    validator = result(verdict="match", confidence=90, reasoning="Transfer of 100 USDT observed.")
    assert mod.results_agree(leader, validator, plan()) is True


def test_a_claim_on_one_side_only_fails(mod):
    assert mod.results_agree(result(verdict="match", confidence=90), result(), plan()) is False


# ---------------------------------------------------------------------------
# Event assertions
# ---------------------------------------------------------------------------


def test_the_same_assertion_outcome_agrees(mod):
    spec = json.dumps({"address": USDT}, sort_keys=True, separators=(",", ":"))
    leader = result(assertion=spec, assertion_satisfied=True)
    validator = result(assertion=spec, assertion_satisfied=True)
    assert mod.results_agree(leader, validator, plan()) is True


def test_differing_assertion_outcomes_fail(mod):
    spec = json.dumps({"address": USDT}, sort_keys=True, separators=(",", ":"))
    leader = result(assertion=spec, assertion_satisfied=True)
    validator = result(assertion=spec, assertion_satisfied=False)
    assert mod.results_agree(leader, validator, plan()) is False


def test_differing_assertions_fail(mod):
    """Guards against a leader quietly answering a different question."""
    leader = result(assertion='{"address":"0xaaa"}', assertion_satisfied=True)
    validator = result(assertion='{"address":"0xbbb"}', assertion_satisfied=True)
    assert mod.results_agree(leader, validator, plan()) is False


def test_match_count_differences_are_rejected(mod):
    """Match counts are derived from agreed logs and are stored on-chain."""
    spec = json.dumps({"address": USDT}, sort_keys=True, separators=(",", ":"))
    leader = result(assertion=spec, assertion_satisfied=True, assertion_log_matched=1)
    validator = result(assertion=spec, assertion_satisfied=True, assertion_log_matched=2)
    assert mod.results_agree(leader, validator, plan()) is False


# ---------------------------------------------------------------------------
# Error comparison
# ---------------------------------------------------------------------------


def make_error(mod, message: str):
    return mod.gl.vm.UserError(message=message)


def test_identical_business_errors_agree(mod):
    """Deterministic rejections are reproducible, so they must match word for
    word."""
    left = make_error(mod, "[EXPECTED] chain 5 is not registered")
    right = make_error(mod, "[EXPECTED] chain 5 is not registered")
    assert mod.compare_errors(left, right) is True


def test_different_business_errors_fail(mod):
    left = make_error(mod, "[EXPECTED] chain 5 is not registered")
    right = make_error(mod, "[EXPECTED] chain 9 is not registered")
    assert mod.compare_errors(left, right) is False


def test_identical_external_errors_agree(mod):
    left = make_error(mod, "[EXTERNAL] source rejected request (403)")
    right = make_error(mod, "[EXTERNAL] source rejected request (403)")
    assert mod.compare_errors(left, right) is True


def test_different_external_status_codes_fail(mod):
    left = make_error(mod, "[EXTERNAL] source rejected request (403)")
    right = make_error(mod, "[EXTERNAL] source rejected request (401)")
    assert mod.compare_errors(left, right) is False


def test_any_two_transient_errors_agree(mod):
    """Network failures are not reproducible, so agreeing that one happened is
    the most that can be asked. The wording is not compared."""
    left = make_error(mod, "[TRANSIENT] source unavailable (503)")
    right = make_error(mod, "[TRANSIENT] request failed: TimeoutError")
    assert mod.compare_errors(left, right) is True


def test_model_errors_never_agree(mod):
    """Agreeing on broken model output would lock bad state in. Disagreeing
    forces a leader rotation instead."""
    left = make_error(mod, "[LLM_ERROR] unusable verdict 'maybe'")
    right = make_error(mod, "[LLM_ERROR] unusable verdict 'maybe'")
    assert mod.compare_errors(left, right) is False


def test_a_model_error_on_either_side_never_agrees(mod):
    llm = make_error(mod, "[LLM_ERROR] non-dict response")
    transient = make_error(mod, "[TRANSIENT] source unavailable (503)")
    assert mod.compare_errors(llm, transient) is False
    assert mod.compare_errors(transient, llm) is False


def test_mismatched_error_classes_fail(mod):
    expected = make_error(mod, "[EXPECTED] claim must not be empty")
    transient = make_error(mod, "[TRANSIENT] source unavailable (503)")
    assert mod.compare_errors(expected, transient) is False


def test_unclassified_errors_never_agree(mod):
    """An error with no class prefix is a bug, and a bug should not reach
    consensus."""
    left = make_error(mod, "something went wrong")
    right = make_error(mod, "something went wrong")
    assert mod.compare_errors(left, right) is False


# ---------------------------------------------------------------------------
# End to end through the real validator closure
#
# These run the contract method (which executes the leader), then swap the
# mocks to simulate a validator observing different external data, then invoke
# the captured validator. This exercises the real consensus path rather than
# calling results_agree directly.
# ---------------------------------------------------------------------------


def test_validator_agrees_when_it_sees_the_same_data(direct_vm, verifier, at_time):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert direct_vm.run_validator() is True


def test_validator_agrees_when_the_chain_has_advanced(direct_vm, verifier, at_time):
    """The realistic case. By the time a validator runs, the chain has moved on
    and every source reports a deeper confirmation count."""
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm, head=BLOCK + 64)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    direct_vm.clear_mocks()
    mock_all_agree(direct_vm, head=BLOCK + 70)

    assert direct_vm.run_validator() is True


def test_validator_disagrees_when_a_source_reports_a_different_amount(
    direct_vm, verifier, at_time
):
    """An explorer that changes a transfer amount between the two reads is
    caught, because settlement is compared exactly once both sides see the
    transaction mined."""
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    direct_vm.clear_mocks()
    tampered = rpc_receipt(logs=[erc20_transfer_log(amount=999)])
    body = evm_batch(rpc_transaction(), tampered, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(confirmations=65),
        blockscout_logs([erc20_transfer_log(amount=999)]),
    )

    assert direct_vm.run_validator() is False


def test_validator_disagrees_when_the_recipient_changes(direct_vm, verifier, at_time):
    """Identity is signed, so a recipient change is never legitimate drift."""
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    direct_vm.clear_mocks()
    other = "0x" + "99" * 20
    body = evm_batch(
        rpc_transaction(to=other), rpc_receipt(to=other), BLOCK + 64
    )
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(to=other, confirmations=65),
        blockscout_logs(),
    )

    assert direct_vm.run_validator() is False


def test_validator_disagrees_when_the_transaction_vanishes(direct_vm, verifier, at_time):
    """A validator that cannot find a transaction the leader confirmed must not
    agree, even though absence is otherwise treated as weak evidence."""
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    direct_vm.clear_mocks()
    empty = evm_batch(None, None, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, empty)
    mock_rpc(direct_vm, BETA, empty)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))

    assert direct_vm.run_validator() is False


def test_validator_disagrees_when_the_leader_result_is_forged(direct_vm, verifier, at_time):
    """Simulates a dishonest leader directly: the external data is unchanged,
    but the leader claims a confirmed verdict for a transaction that the
    sources say does not exist."""
    at_time("2026-01-15T10:30:00Z")
    empty = evm_batch(None, None, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, empty)
    mock_rpc(direct_vm, BETA, empty)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    forged = dict(result(), chain_id=TEST_CHAIN, tx_hash=TX_HASH)
    assert direct_vm.run_validator(leader_result=forged) is False


def test_validator_disagrees_with_a_non_dict_leader_result(direct_vm, verifier, at_time):
    """Defensive: a malformed leader payload is rejected rather than crashing
    the validator."""
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    assert direct_vm.run_validator(leader_result="not a dict") is False


def test_validator_agrees_on_the_same_claim_verdict(direct_vm, verifier, at_time):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="match", confidence=92)
    verifier.verify_claim(TEST_CHAIN, TX_HASH, "100 USDT moved from one account to another")

    assert direct_vm.run_validator() is True


def test_validator_disagrees_on_an_opposite_claim_verdict(direct_vm, verifier, at_time):
    """Two independent model runs reaching opposite conclusions must break
    consensus, which forces a rotation rather than storing a coin flip."""
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="match", confidence=92)
    verifier.verify_claim(TEST_CHAIN, TX_HASH, "100 USDT moved from one account to another")

    direct_vm.clear_mocks()
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="mismatch", confidence=88)

    assert direct_vm.run_validator() is False


def test_validator_agrees_on_the_same_event_proof(direct_vm, verifier, at_time):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": USDT, "signature": "Transfer(address,address,uint256)"})
    )

    assert direct_vm.run_validator() is True


def test_validator_disagrees_when_the_proven_log_disappears(direct_vm, verifier, at_time):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    verifier.prove_event(
        TEST_CHAIN, TX_HASH, json.dumps({"address": USDT, "signature": "Transfer(address,address,uint256)"})
    )

    direct_vm.clear_mocks()
    body = evm_batch(rpc_transaction(), rpc_receipt(logs=[]), BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm, GAMMA, TX_HASH, blockscout_transaction(confirmations=65), blockscout_logs([])
    )

    assert direct_vm.run_validator() is False
