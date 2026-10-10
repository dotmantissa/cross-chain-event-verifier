"""Normalisation helpers.

These run before anything is compared, so a bug here would show up as
validators disagreeing about two renderings of the same fact. Explorers differ
on address casing, on hex versus decimal integers, and on how they spell
absence, and all of that has to collapse to one canonical form.
"""

import pytest

from conftest import SENDER, TX_HASH


# -- transaction identifiers ------------------------------------------------


def test_evm_hash_is_lowercased(mod):
    assert mod._norm_tx_hash(TX_HASH.upper().replace("0X", "0x")) == TX_HASH


def test_evm_hash_tolerates_surrounding_whitespace(mod):
    assert mod._norm_tx_hash(f"  {TX_HASH}  ") == TX_HASH


@pytest.mark.parametrize(
    "bad",
    [
        "0x1234",
        "0x" + "ab" * 31,
        "0x" + "ab" * 33,
        "0x" + "zz" * 32,
    ],
)
def test_malformed_evm_hash_is_rejected(mod, bad):
    with pytest.raises(Exception, match="malformed 32-byte transaction hash"):
        mod._norm_tx_hash(bad)


def test_solana_signature_passes_through_with_case_intact(mod):
    signature = "3nWq9CNwSU2UXDyYP3LdY2f1VVWSvyq8SCuj4W8b9yGibFyjZ8i2APcVVsDyZP9ijvarvwkgUPZQNy26tcRKgYFB"
    assert mod._norm_tx_hash(signature) == signature


def test_non_base58_signature_is_rejected(mod):
    # 0, O, I and l are excluded from the base58 alphabet precisely because
    # they are visually ambiguous.
    with pytest.raises(Exception, match="malformed base58 signature"):
        mod._norm_tx_hash("3nWq9CNwSU2UXDyYP3LdY2f1VVWSvyq8SCuj4W8b9yG0OIl")


def test_too_short_identifier_is_rejected(mod):
    with pytest.raises(Exception, match="malformed transaction identifier"):
        mod._norm_tx_hash("abc")


# -- addresses --------------------------------------------------------------


def test_checksum_address_is_lowercased(mod):
    assert mod._norm_addr("0x23F4569002A5A07F0ECF688142EEB6BCD883EEF8") == SENDER


@pytest.mark.parametrize("empty", [None, "", "0x", "none", "null", "NULL", "  "])
def test_every_spelling_of_absence_collapses_to_empty(mod, empty):
    """Contract creation has no recipient, and explorers each spell that
    differently. They all have to reduce to the same value or the identity
    digest would differ between sources."""
    assert mod._norm_addr(empty) == ""


# -- integers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0x1000000", 16777216),
        ("0X1000000", 16777216),
        ("16777216", 16777216),
        (16777216, 16777216),
        ("0x0", 0),
        ("0", 0),
    ],
)
def test_hex_and_decimal_both_parse(mod, raw, expected):
    assert mod._hex_to_int(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-number", "0xzz"])
def test_unparseable_integers_return_the_default(mod, raw):
    assert mod._hex_to_int(raw, default=-1) == -1


def test_bool_is_not_silently_read_as_an_integer(mod):
    """Python treats bool as a subclass of int. A JSON ``true`` where a number
    was expected is bad data, not the number one."""
    assert mod._hex_to_int(True, default=-1) == -1
    assert mod._hex_to_int(False, default=-1) == -1


def test_value_renders_as_canonical_decimal(mod):
    """RPC reports wei as hex, Blockscout as a decimal string. Both have to
    produce the same text or they would look like different transactions."""
    assert mod._dec_str("0x48d5cc090c1a4000") == "5248325280000000000"
    assert mod._dec_str("5248325280000000000") == "5248325280000000000"
    assert mod._dec_str(None) == "0"


def test_negative_values_clamp_to_zero(mod):
    assert mod._dec_str(-5) == "0"


# -- method selectors -------------------------------------------------------


def test_selector_is_the_first_four_bytes(mod):
    assert mod._selector_of("0xa9059cbb" + "00" * 64) == "0xa9059cbb"


def test_selector_is_lowercased(mod):
    assert mod._selector_of("0xA9059CBB" + "00" * 64) == "0xa9059cbb"


@pytest.mark.parametrize("raw", ["0x", "", None, "0xa905"])
def test_plain_transfers_have_no_selector(mod, raw):
    """A value transfer to an EOA carries no calldata."""
    assert mod._selector_of(raw) == ""


# -- ABI word reading ------------------------------------------------------


def test_reads_each_32_byte_word(mod):
    data = "0x" + format(1, "064x") + format(999, "064x")
    assert mod._word_at(data, 0) == 1
    assert mod._word_at(data, 1) == 999


def test_reading_past_the_end_returns_minus_one(mod):
    assert mod._word_at("0x" + format(1, "064x"), 1) == -1
    assert mod._word_at("0x", 0) == -1


def test_address_is_taken_from_the_low_20_bytes_of_a_topic(mod):
    topic = "0x" + "0" * 24 + SENDER[2:]
    assert mod._addr_from_topic(topic) == SENDER


def test_short_topic_yields_no_address(mod):
    assert mod._addr_from_topic("0x1234") == ""


# -- keccak -----------------------------------------------------------------


def test_topic0_matches_the_canonical_erc20_transfer_hash(mod):
    """The well-known value, so a keccak regression would be caught here."""
    assert (
        mod.topic0_for_signature("Transfer(address,address,uint256)")
        == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )


def test_topic0_matches_the_canonical_approval_hash(mod):
    assert (
        mod.topic0_for_signature("Approval(address,address,uint256)")
        == "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
    )


def test_signature_whitespace_is_trimmed_before_hashing(mod):
    assert mod.topic0_for_signature("  Transfer(address,address,uint256)  ") == (
        mod.topic0_for_signature("Transfer(address,address,uint256)")
    )


def test_empty_signature_is_rejected(mod):
    with pytest.raises(Exception, match="empty event signature"):
        mod.topic0_for_signature("   ")


# -- deterministic clock ----------------------------------------------------


def test_clock_reads_the_transaction_datetime(at_time, mod):
    """The clock has to be the transaction's own datetime. Anything derived
    from wall time would differ between the leader and each validator."""
    at_time("2026-01-15T10:30:00Z")
    assert mod._now_epoch_seconds() == 1768473000


def test_clock_handles_fractional_seconds(at_time, mod):
    """GenVM reports microseconds, as observed live on studionet. Fractional
    seconds are truncated rather than rounded, so every node lands on the same
    integer."""
    at_time("2026-10-10T02:19:40.615407Z")
    assert mod._now_epoch_seconds() == 1791598780


def test_clock_accepts_an_explicit_offset(at_time, mod):
    at_time("2026-01-15T11:30:00+01:00")
    assert mod._now_epoch_seconds() == 1768473000


def test_unparseable_datetime_is_rejected(at_time, mod):
    at_time("not a timestamp")
    with pytest.raises(Exception, match="unparseable transaction datetime"):
        mod._now_epoch_seconds()


def test_epoch_round_trips_to_iso(mod):
    assert mod._iso_from_epoch(1768473000) == "2026-01-15T10:30:00Z"


def test_clip_truncates_without_raising(mod):
    assert mod._clip("abcdef", 3) == "abc"
    assert mod._clip("ab", 10) == "ab"
    assert mod._clip(None, 10) == ""
