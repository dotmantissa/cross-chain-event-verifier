"""Cross-source aggregation: quorum, dispute, and the presence/absence split.

The central asymmetry: a node cannot fabricate a signed transaction, but it can
easily fail to hold one. Public RPC endpoints were observed returning null
receipts for a transaction that demonstrably exists, because they load balance
across partly pruned backends. So positive evidence carries, while declaring a
transaction absent needs a quorum that all agree it is missing.

The other property under test is that disagreement is a *result*, not an error.
When sources contradict each other the record is stored with status
``disputed`` and ``confirmed`` false, so a consumer can see the conflict. If it
raised instead, the caller could simply retry until a flaky source agreed.
"""

import pytest

from conftest import SENDER, USDT, erc20_transfer_log

BLOCK = 16777216


def plan(quorum: int = 2, min_confirmations: int = 12, drift: int = 10) -> dict:
    return {
        "chain_id": "99999",
        "quorum": quorum,
        "min_confirmations": min_confirmations,
        "drift_tolerance": drift,
        "max_sources": 5,
        "sources": [],
        "expected_evm_chain_id": 99999,
        "family": "evm",
    }


def found(
    label: str = "alpha",
    confirmations: int = 64,
    status: str = "success",
    block: int = BLOCK,
    value: str = "0",
    to: str = USDT,
    logs=None,
    tx_index: int = 42,
) -> dict:
    return {
        "label": label,
        "kind": "evm_jsonrpc",
        "outcome": "found",
        "note": "receipt",
        "confirmations": confirmations,
        "head": block + confirmations - 1 if block >= 0 else -1,
        "facts": {
            "status": status,
            "from": SENDER,
            "to": to,
            "value": value,
            "nonce": 79,
            "block_number": block,
            "tx_index": tx_index,
            "selector": "0xa9059cbb",
            "created_contract": "",
            "logs": logs if logs is not None else [erc20_transfer_log()],
            "accounts": [],
            "programs": [],
        },
    }


def absent(label: str = "alpha") -> dict:
    return {
        "label": label,
        "kind": "evm_jsonrpc",
        "outcome": "absent",
        "note": "no transaction",
        "head": BLOCK + 64,
    }


def errored(label: str = "alpha", note: str = "[TRANSIENT] boom") -> dict:
    return {"label": label, "kind": "evm_jsonrpc", "outcome": "error", "note": note}


# -- the happy path ---------------------------------------------------------


def test_two_agreeing_sources_confirm(mod):
    result = mod.aggregate([found("alpha"), found("beta")], plan())
    assert result["exists"] is True
    assert result["mined"] is True
    assert result["confirmed"] is True
    assert result["status"] == "success"
    assert result["finality"] == "final"
    assert result["agreement"] == "strong"
    assert result["sources_agreeing"] == 2
    assert result["sources_queried"] == 2


def test_agreement_survives_differing_confirmation_counts(mod):
    """Confirmation depth is explicitly not part of the identity digest, so
    two sources reading at different heights still agree."""
    result = mod.aggregate([found("alpha", confirmations=64), found("beta", confirmations=97)], plan())
    assert result["confirmed"] is True
    assert result["sources_agreeing"] == 2


def test_confirmation_depth_is_corroborated_by_quorum(mod):
    """Reporting the deepest corroborated confirmation count is the honest
    choice: it is a value some source actually observed."""
    result = mod.aggregate([found("alpha", confirmations=64), found("beta", confirmations=97)], plan())
    assert result["confirmations"] == 64


def test_three_sources_all_counted(mod):
    result = mod.aggregate([found("a"), found("b"), found("c")], plan())
    assert result["sources_agreeing"] == 3
    assert result["sources_positive"] == 3


# -- quorum enforcement -----------------------------------------------------


def test_a_single_source_is_never_enough_to_confirm(mod):
    """One explorer agreeing with itself is not consensus, no matter how deep
    its confirmation count looks."""
    result = mod.aggregate([found("alpha", confirmations=5000)], plan(quorum=2))
    assert result["exists"] is True
    assert result["confirmed"] is False
    assert result["agreement"] == "weak"


def test_quorum_of_one_allows_a_single_source(mod):
    """A chain configured with quorum 1 is explicitly trusting one source."""
    result = mod.aggregate([found("alpha")], plan(quorum=1))
    assert result["confirmed"] is True
    assert result["agreement"] == "strong"


def test_one_agreeing_source_plus_errors_does_not_confirm(mod):
    result = mod.aggregate([found("alpha"), errored("beta"), errored("gamma")], plan(quorum=2))
    assert result["exists"] is True
    assert result["confirmed"] is False
    assert result["agreement"] == "weak"
    assert result["sources_errored"] == 2


# -- disagreement is a result, not an error --------------------------------


def test_an_even_split_on_value_is_disputed(mod):
    """Two sources reporting different amounts for the same hash is the
    scenario the whole design exists to catch."""
    result = mod.aggregate([found("alpha", value="0"), found("beta", value="1000")], plan())
    assert result["status"] == "disputed"
    assert result["confirmed"] is False
    assert result["agreement"] == "none"
    assert result["identity_digest"] == ""


def test_an_even_split_on_recipient_is_disputed(mod):
    result = mod.aggregate(
        [found("alpha", to=USDT), found("beta", to="0x" + "99" * 20)], plan()
    )
    assert result["status"] == "disputed"
    assert result["confirmed"] is False


def test_a_dishonest_minority_loses_to_the_majority(mod):
    """Two honest sources outvote one liar, and the liar does not prevent a
    confirmed verdict."""
    result = mod.aggregate(
        [found("alpha", value="0"), found("beta", value="0"), found("liar", value="999")],
        plan(quorum=2),
    )
    assert result["status"] == "success"
    assert result["confirmed"] is True
    assert result["sources_agreeing"] == 2
    assert result["sources_positive"] == 3


def test_dispute_does_not_raise(mod):
    """Returning a verdict rather than raising is deliberate: an exception
    could be retried until a flaky source happened to agree."""
    result = mod.aggregate([found("a", value="1"), found("b", value="2")], plan())
    assert result["status"] == "disputed"
    assert result["exists"] is True


def test_dispute_withholds_every_fact(mod):
    """Nothing is reported from a disputed read, since there is no basis for
    choosing which source to believe."""
    result = mod.aggregate([found("a", value="1"), found("b", value="2")], plan())
    assert result["from"] == ""
    assert result["to"] == ""
    assert result["value"] == "0"
    assert result["block_number"] == -1
    assert result["log_count"] == 0
    assert result["settlement_digest"] == ""


def test_a_three_way_split_is_disputed(mod):
    result = mod.aggregate(
        [found("a", value="1"), found("b", value="2"), found("c", value="3")], plan()
    )
    assert result["status"] == "disputed"


# -- absence requires a quorum ---------------------------------------------


def test_a_quorum_of_absences_declares_the_transaction_missing(mod):
    result = mod.aggregate([absent("alpha"), absent("beta")], plan(quorum=2))
    assert result["exists"] is False
    assert result["status"] == "absent"
    assert result["confirmed"] is False
    assert result["agreement"] == "strong"
    assert result["sources_agreeing"] == 2


def test_a_single_absence_is_not_enough(mod):
    """The finding that drove this: one endpoint reporting nothing is weak
    evidence, because it may simply be a pruned backend."""
    with pytest.raises(Exception, match="needed to declare a transaction absent"):
        mod.aggregate([absent("alpha")], plan(quorum=2))


def test_one_absence_plus_one_error_is_not_enough(mod):
    with pytest.raises(Exception, match="needed to declare a transaction absent"):
        mod.aggregate([absent("alpha"), errored("beta")], plan(quorum=2))


def test_one_positive_outweighs_two_absences(mod):
    """Asymmetry in action. A source cannot invent a signed transaction, so
    positive evidence beats any amount of absence."""
    result = mod.aggregate([found("alpha"), absent("beta"), absent("gamma")], plan(quorum=2))
    assert result["exists"] is True
    assert result["sources_absent"] == 2
    # Still not confirmed, because only one source corroborates the facts.
    assert result["confirmed"] is False
    assert result["agreement"] == "weak"


def test_two_positives_and_one_absence_confirm(mod):
    result = mod.aggregate([found("alpha"), found("beta"), absent("gamma")], plan(quorum=2))
    assert result["exists"] is True
    assert result["confirmed"] is True


def test_every_source_failing_raises_as_transient(mod):
    """No evidence at all is not the same as evidence of absence, so this is a
    transient failure the caller can retry rather than a stored verdict."""
    with pytest.raises(Exception, match=r"\[TRANSIENT\] every source failed"):
        mod.aggregate([errored("alpha"), errored("beta")], plan())


# -- finality and status ---------------------------------------------------


def test_too_few_confirmations_is_confirming_not_final(mod):
    result = mod.aggregate(
        [found("alpha", confirmations=3), found("beta", confirmations=3)], plan(min_confirmations=12)
    )
    assert result["finality"] == "confirming"
    assert result["confirmed"] is False
    assert result["mined"] is True


def test_exactly_the_threshold_is_final(mod):
    result = mod.aggregate(
        [found("alpha", confirmations=12), found("beta", confirmations=12)],
        plan(min_confirmations=12),
    )
    assert result["finality"] == "final"
    assert result["confirmed"] is True


def test_a_reverted_transaction_exists_but_is_not_confirmed(mod):
    """Existence and success are separate questions. A reverted transaction
    really happened, and consumers must not treat it as a successful one."""
    result = mod.aggregate(
        [found("alpha", status="failed"), found("beta", status="failed")], plan()
    )
    assert result["exists"] is True
    assert result["mined"] is True
    assert result["status"] == "failed"
    assert result["confirmed"] is False


def test_a_pending_transaction_exists_but_is_not_mined(mod):
    result = mod.aggregate(
        [
            found("alpha", status="pending", block=-1, confirmations=0),
            found("beta", status="pending", block=-1, confirmations=0),
        ],
        plan(),
    )
    assert result["exists"] is True
    assert result["mined"] is False
    assert result["finality"] == "pending"
    assert result["confirmed"] is False


def test_near_threshold_is_flagged_just_below(mod):
    """Readings inside the hysteresis band are marked so a consumer wanting
    certainty can reject them via is_verified_strict."""
    result = mod.aggregate(
        [found("alpha", confirmations=8), found("beta", confirmations=8)],
        plan(min_confirmations=12, drift=10),
    )
    assert result["near_threshold"] is True


def test_near_threshold_is_flagged_just_above(mod):
    result = mod.aggregate(
        [found("alpha", confirmations=14), found("beta", confirmations=14)],
        plan(min_confirmations=12, drift=10),
    )
    assert result["confirmed"] is True
    assert result["near_threshold"] is True


def test_deep_confirmation_is_not_near_threshold(mod):
    result = mod.aggregate(
        [found("alpha", confirmations=5000), found("beta", confirmations=5000)],
        plan(min_confirmations=12, drift=10),
    )
    assert result["confirmed"] is True
    assert result["near_threshold"] is False


# -- the pruned-backend case ------------------------------------------------


def test_a_source_without_a_receipt_does_not_block_consensus(mod):
    """Reproduces the observed failure. One backend sees the transaction but
    has no receipt; another has both. The one with settlement is preferred."""
    no_receipt = found("alpha", status="pending", confirmations=0)
    no_receipt["facts"]["logs"] = []
    no_receipt["facts"]["tx_index"] = -1
    no_receipt["note"] = "no receipt on this backend"

    result = mod.aggregate([no_receipt, found("beta"), found("gamma")], plan(quorum=2))
    assert result["exists"] is True
    assert result["mined"] is True
    assert result["status"] == "success"
    assert result["confirmed"] is True
    assert result["log_count"] == 1


def test_only_pruned_sources_means_mined_but_unsettled(mod):
    """If no source has a receipt, the transaction is reported as existing and
    pending rather than as successful. Inclusion is not settlement."""
    first = found("alpha", status="pending", confirmations=0)
    first["facts"]["logs"] = []
    second = found("beta", status="pending", confirmations=0)
    second["facts"]["logs"] = []

    result = mod.aggregate([first, second], plan(quorum=2))
    assert result["exists"] is True
    assert result["confirmed"] is False
    assert result["status"] == "pending"


def test_settlement_disagreement_blocks_confirmation(mod):
    """Two sources sharing an identity but disagreeing about the logs must not
    produce a confirmed verdict."""
    result = mod.aggregate(
        [
            found("alpha", logs=[erc20_transfer_log(amount=1)]),
            found("beta", logs=[erc20_transfer_log(amount=2)]),
            found("gamma", logs=[erc20_transfer_log(amount=3)]),
        ],
        plan(quorum=2),
    )
    assert result["exists"] is True
    assert result["confirmed"] is False


def test_settlement_majority_confirms(mod):
    result = mod.aggregate(
        [
            found("alpha", logs=[erc20_transfer_log(amount=1)]),
            found("beta", logs=[erc20_transfer_log(amount=1)]),
            found("gamma", logs=[erc20_transfer_log(amount=3)]),
        ],
        plan(quorum=2),
    )
    assert result["confirmed"] is True
    assert result["log_count"] == 1


# -- reporting --------------------------------------------------------------


def test_every_source_appears_in_the_report(mod):
    """The per-source report is the audit trail. It records what each source
    said, including the ones that failed."""
    result = mod.aggregate(
        [found("alpha"), absent("beta"), errored("gamma", "[TRANSIENT] timeout")], plan()
    )
    labels = {r["label"]: r for r in result["source_reports"]}
    assert labels["alpha"]["outcome"] == "found"
    assert labels["beta"]["outcome"] == "absent"
    assert labels["gamma"]["outcome"] == "error"
    assert "timeout" in labels["gamma"]["note"]


def test_counts_add_up(mod):
    result = mod.aggregate(
        [found("a"), found("b"), absent("c"), errored("d")], plan(quorum=2)
    )
    assert result["sources_queried"] == 4
    assert result["sources_positive"] == 2
    assert result["sources_absent"] == 1
    assert result["sources_errored"] == 1


def test_derive_finality_covers_every_state(mod):
    assert mod.derive_finality(False, False, 0, 12) == "unknown"
    assert mod.derive_finality(True, False, 0, 12) == "pending"
    assert mod.derive_finality(True, True, 3, 12) == "confirming"
    assert mod.derive_finality(True, True, 12, 12) == "final"
    assert mod.derive_finality(True, True, 99, 12) == "final"
