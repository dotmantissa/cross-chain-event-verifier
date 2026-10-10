"""Natural-language claim adjudication.

A language model is involved here, but its authority is deliberately narrow.
It never decides whether a transaction exists or is confirmed; those are
computed by code from source data that validators already agreed on. The model
answers one question only: do the submitter's words match those facts.

That boundary is what the first group of tests pins. The second group covers
defensive parsing, because a model will not reliably return the shape it was
asked for.
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
CLAIM = "100 USDT was transferred from one address to another"


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_matching_claim_is_recorded(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="match", confidence=93, reasoning="Transfer found.")

    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)
    assert out["verdict"] == "match"
    assert out["confidence"] == 93
    assert out["tx_confirmed"] is True
    assert out["valid"] is True
    assert out["reasoning"] == "Transfer found."


def test_the_transaction_record_comes_back_alongside_the_claim(direct_vm, verifier, at_time):
    """One consensus round produces both, so a consumer does not have to make
    a second call to learn whether the transaction was confirmed."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm)

    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)
    assert out["transaction"]["confirmed"] is True
    assert out["transaction"]["from"] == SENDER
    assert verifier.is_verified(TEST_CHAIN, TX_HASH) is True


def test_is_claim_verified_answers_true(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm)
    verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)

    assert verifier.is_claim_verified(TEST_CHAIN, TX_HASH, CLAIM) is True


def test_a_mismatching_claim_is_recorded_as_such(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="mismatch", confidence=90, reasoning="Wrong amount.")

    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, "1000 ETH was sent to a casino")
    assert out["verdict"] == "mismatch"
    assert out["valid"] is False
    assert verifier.is_claim_verified(TEST_CHAIN, TX_HASH, "1000 ETH was sent to a casino") is False


def test_an_indeterminate_claim_is_not_valid(direct_vm, verifier, at_time):
    """Not provable from the facts is a distinct answer from contradicted, and
    neither releases funds."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="indeterminate", confidence=20)

    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, "the sender intended this as a gift")
    assert out["verdict"] == "indeterminate"
    assert out["valid"] is False


def test_claims_are_keyed_independently(direct_vm, verifier, at_time):
    """Two different claims about the same transaction are separate records."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="match")
    verifier.verify_claim(TEST_CHAIN, TX_HASH, "claim one")

    direct_vm.clear_mocks()
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm, verdict="mismatch")
    verifier.verify_claim(TEST_CHAIN, TX_HASH, "claim two")

    assert verifier.is_claim_verified(TEST_CHAIN, TX_HASH, "claim one") is True
    assert verifier.is_claim_verified(TEST_CHAIN, TX_HASH, "claim two") is False


def test_an_unadjudicated_claim_reads_as_unverified(verifier):
    out = verifier.get_claim(TEST_CHAIN, TX_HASH, "never asked")
    assert out["found"] is False
    assert out["verdict"] == "unverified"
    assert out["valid"] is False


# ---------------------------------------------------------------------------
# The model's authority is bounded
# ---------------------------------------------------------------------------


def test_a_matching_claim_on_an_unconfirmed_transaction_is_not_valid(
    direct_vm, verifier, at_time
):
    """The decisive test. Even if the model says the words match, a claim about
    a transaction that is not confirmed cannot be relied on. ``valid`` requires
    both, and only the confirmation half is under code's control."""
    at_time(NOW)
    body = evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 2)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm, GAMMA, TX_HASH, blockscout_transaction(confirmations=3), blockscout_logs()
    )
    mock_llm_verdict(direct_vm, verdict="match", confidence=99)

    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)
    assert out["verdict"] == "match"
    assert out["tx_confirmed"] is False
    assert out["valid"] is False
    assert verifier.is_claim_verified(TEST_CHAIN, TX_HASH, CLAIM) is False


def test_an_absent_transaction_is_decided_without_the_model(direct_vm, verifier, at_time):
    """When a quorum of sources reports nothing, there is nothing to adjudicate
    and no model is consulted. Note that no LLM mock is registered: if the
    contract tried to call one, this test would fail with a missing mock."""
    at_time(NOW)
    empty = evm_batch(None, None, BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, empty)
    mock_rpc(direct_vm, BETA, empty)
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))

    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)
    assert out["verdict"] == "mismatch"
    assert out["confidence"] == 100
    assert "no such transaction" in out["reasoning"]
    assert out["valid"] is False


def test_a_disputed_transaction_is_decided_without_the_model(direct_vm, verifier, at_time):
    """Sources contradicting each other leaves no facts to judge a claim
    against, so the answer is indeterminate by construction."""
    at_time(NOW)
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(value=0), rpc_receipt(), BLOCK + 64))
    mock_rpc(direct_vm, BETA, evm_batch(rpc_transaction(value=10**18), rpc_receipt(), BLOCK + 64))
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))

    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)
    assert out["verdict"] == "indeterminate"
    assert out["confidence"] == 0
    assert "disagree" in out["reasoning"]


def test_the_ground_truth_block_carries_only_derived_facts(direct_vm, verifier, mod, at_time):
    """What the model is shown is itself a security property: it sees facts
    that code derived and validators agreed on, and nothing else."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm)
    out = verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)

    rendered = mod.render_ground_truth(
        {
            "chain_id": TEST_CHAIN,
            "tx_hash": TX_HASH,
            "exists": True,
            "status": "success",
            "confirmed": True,
            "finality": "final",
            "confirmations": 65,
            "block_number": BLOCK,
            "from": SENDER,
            "to": USDT,
            "value": "0",
            "selector": "0xa9059cbb",
            "created_contract": "",
            "log_count": 1,
            "sources_agreeing": 2,
            "sources_queried": 2,
        },
        out["transaction"]["decoded_events"],
    )
    assert "confirmed_by_consensus: True" in rendered
    assert "ERC20Transfer" in rendered
    assert "value=100000000" in rendered
    assert f"sender: {SENDER}" in rendered


def test_ground_truth_reports_when_nothing_was_decoded(mod):
    """An honest empty answer, so the model cannot infer an event from silence."""
    rendered = mod.render_ground_truth(
        {
            "exists": True,
            "status": "success",
            "confirmed": True,
            "finality": "final",
            "confirmations": 20,
            "block_number": BLOCK,
            "from": SENDER,
            "to": USDT,
            "value": "0",
            "selector": "",
            "created_contract": "",
            "log_count": 0,
            "sources_agreeing": 2,
            "sources_queried": 3,
        },
        [],
    )
    assert "decoded_events: (none)" in rendered


# ---------------------------------------------------------------------------
# Defensive parsing of the model's answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"verdict": "match"}, "match"),
        ({"verdict": "MATCH"}, "match"),
        ({"verdict": "Matches"}, "match"),
        ({"verdict": "matched"}, "match"),
        ({"verdict": "true"}, "match"),
        ({"verdict": "yes"}, "match"),
        ({"verdict": "confirmed"}, "match"),
        ({"verdict": True}, "match"),
        ({"verdict": "mismatch"}, "mismatch"),
        ({"verdict": "contradicted"}, "mismatch"),
        ({"verdict": "no"}, "mismatch"),
        ({"verdict": False}, "mismatch"),
        ({"verdict": "indeterminate"}, "indeterminate"),
        ({"verdict": "unclear"}, "indeterminate"),
        ({"verdict": "insufficient"}, "indeterminate"),
        ({"result": "match"}, "match"),
        ({"decision": "match"}, "match"),
        ({"answer": "match"}, "match"),
        ({"match": True}, "match"),
    ],
)
def test_verdict_synonyms_are_accepted(mod, raw, expected):
    """Models paraphrase. Accepting the obvious synonyms costs nothing and
    avoids forcing a rotation over wording."""
    assert mod.parse_adjudication(raw)["verdict"] == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"verdict": "match", "confidence": 90}, 90),
        ({"verdict": "match", "confidence": "90"}, 90),
        ({"verdict": "match", "confidence": "90%"}, 90),
        ({"verdict": "match", "confidence": 90.4}, 90),
        # A fraction is scaled before rounding. Rounding first would turn 0.9
        # into 1 and then into 100, inflating the answer.
        ({"verdict": "match", "confidence": 0.9}, 90),
        ({"verdict": "match", "confidence": 0.5}, 50),
        ({"verdict": "match", "confidence": 0.05}, 5),
        # An exact 1 is read as one percent, not as certainty. The prompt asks
        # for 0..100, so a bare 1 is much more likely to be a low integer than
        # a fraction, and guessing the other way would inflate it to 100.
        ({"verdict": "match", "confidence": 1}, 1),
        ({"verdict": "match", "confidence": 0}, 0),
        ({"verdict": "match", "confidence": 150}, 100),
        ({"verdict": "match", "confidence": -5}, 0),
        ({"verdict": "match", "certainty": 80}, 80),
        ({"verdict": "match"}, 50),
        ({"verdict": "match", "confidence": "high"}, 50),
        ({"verdict": "match", "confidence": None}, 50),
    ],
)
def test_confidence_is_coerced_into_range(mod, raw, expected):
    """A fraction is read as a fraction, a percentage as a percentage, and
    anything unparseable falls back to neutral rather than raising."""
    assert mod.parse_adjudication(raw)["confidence"] == expected


def test_a_non_dict_answer_is_an_llm_error(mod):
    """Classified as LLM_ERROR specifically so validators never agree on it,
    which forces a rotation instead of storing garbage."""
    with pytest.raises(Exception, match=r"\[LLM_ERROR\] adjudication was str"):
        mod.parse_adjudication("match")


def test_an_unusable_verdict_is_an_llm_error(mod):
    with pytest.raises(Exception, match=r"\[LLM_ERROR\] unusable verdict"):
        mod.parse_adjudication({"verdict": "perhaps on a tuesday"})


def test_a_missing_verdict_is_an_llm_error(mod):
    with pytest.raises(Exception, match=r"\[LLM_ERROR\] unusable verdict"):
        mod.parse_adjudication({"confidence": 90, "reasoning": "looks fine"})


def test_reasoning_is_truncated_not_rejected(mod):
    out = mod.parse_adjudication(
        {"verdict": "match", "confidence": 90, "reasoning": "x" * 5000}
    )
    assert len(out["reasoning"]) == mod.MAX_REASONING_CHARS


def test_reasoning_falls_back_to_explanation(mod):
    out = mod.parse_adjudication(
        {"verdict": "match", "confidence": 90, "explanation": "because of the log"}
    )
    assert out["reasoning"] == "because of the log"


def test_missing_reasoning_is_tolerated(mod):
    out = mod.parse_adjudication({"verdict": "match", "confidence": 90})
    assert out["reasoning"] == ""


# ---------------------------------------------------------------------------
# Claim input validation
# ---------------------------------------------------------------------------


def test_an_empty_claim_is_rejected(verifier):
    with pytest.raises(Exception, match=r"\[EXPECTED\] claim must not be empty"):
        verifier.verify_claim(TEST_CHAIN, TX_HASH, "   ")


def test_an_oversized_claim_is_rejected(verifier, mod):
    """Bounded so one submission cannot blow the compute budget."""
    with pytest.raises(Exception, match=r"\[EXPECTED\] claim is \d+ characters"):
        verifier.verify_claim(TEST_CHAIN, TX_HASH, "x" * (mod.MAX_CLAIM_CHARS + 1))


def test_claim_whitespace_is_normalised_for_the_cache_key(direct_vm, verifier, at_time):
    """Trailing whitespace must not create a second record for the same claim."""
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm)
    verifier.verify_claim(TEST_CHAIN, TX_HASH, f"  {CLAIM}  ")

    assert verifier.is_claim_verified(TEST_CHAIN, TX_HASH, CLAIM) is True


def test_claim_statistics_are_counted(direct_vm, verifier, at_time):
    at_time(NOW)
    mock_all_agree(direct_vm)
    mock_llm_verdict(direct_vm)
    verifier.verify_claim(TEST_CHAIN, TX_HASH, CLAIM)

    assert verifier.stats()["claims"] == 1
