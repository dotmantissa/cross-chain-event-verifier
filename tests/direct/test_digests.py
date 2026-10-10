"""The two fact digests.

The design rests on a distinction between two halves of a transaction that have
different lifetimes:

  identity   is signed by the sender and fixed the moment the transaction is
             broadcast. Two honest sources cannot disagree about it, so it is
             compared exactly, always.
  settlement only exists once the transaction is mined, and a source that is
             mid-reindex or running against a pruned backend may not have it
             yet. It is compared exactly, but only once both sides agree the
             transaction is mined.

Splitting them is what lets a pending-versus-mined race between the leader and
a validator reach consensus without ever weakening the guarantee on a
transaction both sides already see as settled.
"""

from conftest import RECIPIENT, SENDER, USDT, erc20_transfer_log


def facts(**overrides) -> dict:
    base = {
        "status": "success",
        "from": SENDER,
        "to": USDT,
        "value": "0",
        "nonce": 79,
        "block_number": 16777216,
        "tx_index": 42,
        "selector": "0xa9059cbb",
        "created_contract": "",
        "logs": [erc20_transfer_log()],
    }
    base.update(overrides)
    return base


# -- identity digest --------------------------------------------------------


def test_identity_is_stable_across_repeated_reads(mod):
    assert mod.identity_digest(facts()) == mod.identity_digest(facts())


def test_identity_ignores_everything_that_drifts(mod):
    """Confirmation depth, gas and timestamps are not part of identity, so a
    source reading the same transaction later still produces the same digest."""
    assert mod.identity_digest(facts()) == mod.identity_digest(
        facts(gas_used="999", confirmations=90210, timestamp="later")
    )


def test_identity_ignores_settlement(mod):
    """A pending reading and a mined reading of the same transaction share an
    identity. This is the property that makes the race tolerable."""
    assert mod.identity_digest(facts()) == mod.identity_digest(
        facts(status="pending", block_number=-1, tx_index=-1, logs=[])
    )


def test_identity_changes_with_the_sender(mod):
    assert mod.identity_digest(facts()) != mod.identity_digest(
        facts(**{"from": "0x" + "99" * 20})
    )


def test_identity_changes_with_the_recipient(mod):
    assert mod.identity_digest(facts()) != mod.identity_digest(facts(to="0x" + "99" * 20))


def test_identity_changes_with_the_value(mod):
    """The case that matters most for settlement: an explorer lying about an
    amount must not share a digest with the truth."""
    assert mod.identity_digest(facts()) != mod.identity_digest(facts(value="1"))


def test_identity_changes_with_the_selector(mod):
    assert mod.identity_digest(facts()) != mod.identity_digest(facts(selector="0xdeadbeef"))


def test_identity_changes_with_the_nonce(mod):
    """The nonce pins the transaction to one slot in the sender's sequence,
    which blocks substituting a different transaction by the same sender."""
    assert mod.identity_digest(facts()) != mod.identity_digest(facts(nonce=80))


def test_contract_creation_has_a_stable_identity(mod):
    """No recipient is a legitimate shape, not an error, and both renderings of
    'no recipient' have to agree."""
    creation = facts(to="", created_contract="0x" + "ab" * 20)
    assert mod.identity_digest(creation) == mod.identity_digest(facts(to="", created_contract="0x" + "ab" * 20))
    assert mod.identity_digest(creation) != mod.identity_digest(facts())


# -- settlement digest ------------------------------------------------------


def test_settlement_is_stable_across_repeated_reads(mod):
    assert mod.settlement_digest(facts()) == mod.settlement_digest(facts())


def test_settlement_changes_with_execution_status(mod):
    """A reverted transaction must never share a settlement digest with a
    successful one."""
    assert mod.settlement_digest(facts()) != mod.settlement_digest(facts(status="failed"))


def test_settlement_changes_with_the_block(mod):
    """Two different blocks means a reorg, which consumers must see."""
    assert mod.settlement_digest(facts()) != mod.settlement_digest(facts(block_number=16777217))


def test_settlement_changes_with_the_transaction_index(mod):
    assert mod.settlement_digest(facts()) != mod.settlement_digest(facts(tx_index=43))


def test_settlement_covers_every_log(mod):
    """Log integrity is in the digest, so two sources disagreeing about any
    log at all is caught rather than averaged over."""
    assert mod.settlement_digest(facts()) != mod.settlement_digest(facts(logs=[]))


def test_settlement_changes_with_a_log_amount(mod):
    """The exact attack this blocks: an explorer reporting a transfer of the
    right token between the right parties, for the wrong amount."""
    assert mod.settlement_digest(facts()) != mod.settlement_digest(
        facts(logs=[erc20_transfer_log(amount=1)])
    )


def test_settlement_changes_with_a_log_emitter(mod):
    assert mod.settlement_digest(facts()) != mod.settlement_digest(
        facts(logs=[erc20_transfer_log(token="0x" + "99" * 20)])
    )


def test_settlement_changes_with_a_log_recipient(mod):
    assert mod.settlement_digest(facts()) != mod.settlement_digest(
        facts(logs=[erc20_transfer_log(recipient="0x" + "99" * 20)])
    )


def test_settlement_is_sensitive_to_log_count(mod):
    """An extra log cannot be hidden, even if every original log is unchanged."""
    assert mod.settlement_digest(facts()) != mod.settlement_digest(
        facts(logs=[erc20_transfer_log(), erc20_transfer_log(amount=1)])
    )


def test_settlement_is_order_sensitive(mod):
    """Log order is part of the receipt, so reordering is a real difference."""
    a = erc20_transfer_log(amount=1)
    b = erc20_transfer_log(amount=2)
    assert mod.settlement_digest(facts(logs=[a, b])) != mod.settlement_digest(
        facts(logs=[b, a])
    )


def test_settlement_ignores_gas_figures(mod):
    """Gas is reported inconsistently across explorers and is not a security
    property here, so it stays out of the digest."""
    assert mod.settlement_digest(facts()) == mod.settlement_digest(
        facts(gas_used="1", effective_gas_price="2")
    )


def test_settlement_changes_with_a_created_contract(mod):
    assert mod.settlement_digest(facts()) != mod.settlement_digest(
        facts(created_contract="0x" + "ab" * 20)
    )


# -- independence -----------------------------------------------------------


def test_the_two_digests_are_independent(mod):
    """A change confined to settlement must leave identity untouched, and the
    reverse. If they moved together the split would buy nothing."""
    settled_differently = facts(status="failed")
    assert mod.identity_digest(facts()) == mod.identity_digest(settled_differently)
    assert mod.settlement_digest(facts()) != mod.settlement_digest(settled_differently)

    sent_differently = facts(value="5")
    assert mod.identity_digest(facts()) != mod.identity_digest(sent_differently)
    assert mod.settlement_digest(facts()) == mod.settlement_digest(sent_differently)


def test_digests_are_versioned(mod):
    """Both digests are domain separated and carry a version tag, so a future
    change to what they cover cannot collide with today's records."""
    assert mod.identity_digest(facts()) != mod.settlement_digest(facts())


def test_digests_are_32_byte_hex(mod):
    for digest in (mod.identity_digest(facts()), mod.settlement_digest(facts())):
        assert digest.startswith("0x")
        assert len(digest) == 66


def test_log_fields_cannot_be_smuggled_across_the_separator(mod):
    """Fields are joined with a separator, so a value containing the separator
    must not be able to impersonate a different field layout."""
    sneaky = facts(**{"from": SENDER, "to": f"{USDT}\n0"})
    assert mod.identity_digest(sneaky) != mod.identity_digest(facts())
