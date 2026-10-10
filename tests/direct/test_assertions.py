"""Structured event assertions, the deterministic verification mode.

An assertion is matched against the receipt's logs in code, with no language
model anywhere in the path. Everything here is pure, so it needs no consensus
of its own; it is the thing consensus is reached about.

Canonicalisation matters as much as matching: the canonical form is the cache
key, so two spellings of the same assertion have to produce the same string or
a consumer would miss a proof that already exists.
"""

import json

import pytest

from conftest import (
    RECIPIENT,
    SENDER,
    TRANSFER_TOPIC,
    USDT,
    erc20_transfer_log,
    erc721_transfer_log,
    topic_pad,
    word,
)


# -- parsing and validation -------------------------------------------------


def test_signature_is_turned_into_topic0(mod):
    spec = mod.parse_assertion(
        json.dumps({"signature": "Transfer(address,address,uint256)"})
    )
    assert spec["topic0"] == TRANSFER_TOPIC
    assert spec["signature"] == "Transfer(address,address,uint256)"


def test_explicit_topic0_is_accepted(mod):
    spec = mod.parse_assertion(json.dumps({"topic0": TRANSFER_TOPIC}))
    assert spec["topic0"] == TRANSFER_TOPIC


def test_signature_and_matching_topic0_together_are_fine(mod):
    spec = mod.parse_assertion(
        json.dumps(
            {"signature": "Transfer(address,address,uint256)", "topic0": TRANSFER_TOPIC}
        )
    )
    assert spec["topic0"] == TRANSFER_TOPIC


def test_signature_contradicting_topic0_is_rejected(mod):
    """Catching this early matters: silently preferring one over the other
    would verify something the builder did not ask for."""
    with pytest.raises(Exception, match="contradicts its signature"):
        mod.parse_assertion(
            json.dumps(
                {
                    "signature": "Transfer(address,address,uint256)",
                    "topic0": "0x" + "11" * 32,
                }
            )
        )


def test_address_is_lowercased(mod):
    spec = mod.parse_assertion(json.dumps({"address": USDT.upper().replace("0X", "0x")}))
    assert spec["address"] == USDT


def test_bare_address_in_a_topic_slot_is_padded(mod):
    """Builders naturally write a 20 byte address where a 32 byte topic word is
    required. Accepting and padding it removes a sharp edge."""
    spec = mod.parse_assertion(json.dumps({"topics": {"1": SENDER}}))
    assert spec["topics"]["1"] == topic_pad(SENDER)


def test_full_topic_word_is_kept_as_is(mod):
    spec = mod.parse_assertion(json.dumps({"topics": {"3": "0x" + word(7804)}}))
    assert spec["topics"]["3"] == "0x" + word(7804)


def test_data_fragment_is_normalised_without_the_prefix(mod):
    spec = mod.parse_assertion(json.dumps({"data_contains": "0xDEADBEEF"}))
    assert spec["data_contains"] == "deadbeef"


def test_min_count_defaults_to_one(mod):
    spec = mod.parse_assertion(json.dumps({"address": USDT}))
    assert spec["min_count"] == 1


def test_min_count_below_one_is_clamped(mod):
    spec = mod.parse_assertion(json.dumps({"address": USDT, "min_count": 0}))
    assert spec["min_count"] == 1


@pytest.mark.parametrize(
    "payload,message",
    [
        ("{not json", "not valid JSON"),
        (json.dumps([1, 2]), "must be a JSON object"),
        (json.dumps({}), "at least one of"),
        (json.dumps({"min_count": 3}), "at least one of"),
        (json.dumps({"address": "0x1234"}), "not 20 bytes"),
        (json.dumps({"topic0": "0xabcd"}), "not 32 bytes"),
        (json.dumps({"topics": [1]}), "topics must be an object"),
        (json.dumps({"topics": {"0": "0x" + word(1)}}), "must be 1..3"),
        (json.dumps({"topics": {"4": "0x" + word(1)}}), "must be 1..3"),
        (json.dumps({"topics": {"x": "0x" + word(1)}}), "is not an index"),
        (json.dumps({"topics": {"1": "0xzz"}}), "must be 20 or 32 bytes"),
        (json.dumps({"data_contains": ""}), "data_contains is empty"),
    ],
)
def test_malformed_assertions_are_rejected(mod, payload, message):
    with pytest.raises(Exception, match=message):
        mod.parse_assertion(payload)


def test_topic0_slot_zero_is_rejected_in_favour_of_the_topic0_field(mod):
    """Slot 0 is the signature hash. It has its own field, so allowing it in
    the topics map would create two ways to say the same thing."""
    with pytest.raises(Exception, match="must be 1..3"):
        mod.parse_assertion(json.dumps({"topics": {"0": TRANSFER_TOPIC}}))


# -- canonicalisation -------------------------------------------------------


def test_key_order_does_not_change_the_canonical_form(mod):
    first = mod.canonical_assertion(
        mod.parse_assertion(json.dumps({"address": USDT, "topic0": TRANSFER_TOPIC}))
    )
    second = mod.canonical_assertion(
        mod.parse_assertion(json.dumps({"topic0": TRANSFER_TOPIC, "address": USDT}))
    )
    assert first == second


def test_signature_and_topic0_spellings_differ_in_canonical_form(mod):
    """Giving the signature records more intent than giving the raw hash, so
    the two are not the same assertion and should not share a cache entry."""
    by_signature = mod.canonical_assertion(
        mod.parse_assertion(json.dumps({"signature": "Transfer(address,address,uint256)"}))
    )
    by_topic = mod.canonical_assertion(
        mod.parse_assertion(json.dumps({"topic0": TRANSFER_TOPIC}))
    )
    assert by_signature != by_topic
    assert json.loads(by_signature)["topic0"] == json.loads(by_topic)["topic0"]


def test_address_casing_does_not_change_the_canonical_form(mod):
    lower = mod.canonical_assertion(mod.parse_assertion(json.dumps({"address": USDT})))
    upper = mod.canonical_assertion(
        mod.parse_assertion(json.dumps({"address": USDT.upper().replace("0X", "0x")}))
    )
    assert lower == upper


def test_canonical_form_is_compact(mod):
    canonical = mod.canonical_assertion(mod.parse_assertion(json.dumps({"address": USDT})))
    assert " " not in canonical


def test_view_exposes_the_same_canonical_form(verifier, mod):
    payload = json.dumps({"address": USDT, "signature": "Transfer(address,address,uint256)"})
    assert verifier.normalize_assertion(payload) == mod.canonical_assertion(
        mod.parse_assertion(payload)
    )


# -- matching ---------------------------------------------------------------


@pytest.fixture
def logs():
    return [
        erc20_transfer_log(amount=100000000),
        erc721_transfer_log(token_id=7804),
        {"address": "0x" + "cc" * 20, "topics": ["0x" + "ee" * 32], "data": "0xfeed"},
    ]


def evaluate(mod, logs, payload):
    return mod.evaluate_assertion(logs, mod.parse_assertion(json.dumps(payload)))


def test_matching_by_emitter_address(mod, logs):
    satisfied, matches = evaluate(mod, logs, {"address": USDT})
    assert satisfied is True
    assert matches == [0]


def test_matching_by_signature_finds_both_transfer_shapes(mod, logs):
    """topic0 alone does not distinguish ERC-20 from ERC-721, so an assertion
    on the signature matches both. Narrowing is the builder's job."""
    satisfied, matches = evaluate(mod, logs, {"signature": "Transfer(address,address,uint256)"})
    assert satisfied is True
    assert matches == [0, 1]


def test_narrowing_by_topic_count_via_an_indexed_token_id(mod, logs):
    satisfied, matches = evaluate(
        mod,
        logs,
        {"signature": "Transfer(address,address,uint256)", "topics": {"3": "0x" + word(7804)}},
    )
    assert satisfied is True
    assert matches == [1]


def test_matching_on_an_indexed_recipient(mod, logs):
    satisfied, matches = evaluate(
        mod, logs, {"address": USDT, "topics": {"2": RECIPIENT}}
    )
    assert satisfied is True
    assert matches == [0]


def test_wrong_recipient_does_not_match(mod, logs):
    satisfied, matches = evaluate(
        mod, logs, {"address": USDT, "topics": {"2": "0x" + "99" * 20}}
    )
    assert satisfied is False
    assert matches == []


def test_matching_an_exact_amount_in_data(mod, logs):
    satisfied, matches = evaluate(
        mod, logs, {"address": USDT, "data_contains": "0x" + word(100000000)}
    )
    assert satisfied is True
    assert matches == [0]


def test_wrong_amount_does_not_match(mod, logs):
    satisfied, _ = evaluate(
        mod, logs, {"address": USDT, "data_contains": "0x" + word(999)}
    )
    assert satisfied is False


def test_data_fragment_matching_is_case_insensitive(mod):
    logs = [{"address": USDT, "topics": [TRANSFER_TOPIC], "data": "0xDEADBEEF"}]
    satisfied, _ = evaluate(mod, logs, {"address": USDT, "data_contains": "deadbeef"})
    assert satisfied is True


def test_all_criteria_must_hold_together(mod, logs):
    """Criteria are conjunctive. An assertion that is individually satisfiable
    on different logs is not satisfied unless one log meets all of it."""
    satisfied, _ = evaluate(
        mod,
        logs,
        {"address": USDT, "topics": {"3": "0x" + word(7804)}},
    )
    assert satisfied is False


def test_min_count_requires_enough_matches(mod):
    pair = [erc20_transfer_log(amount=1), erc20_transfer_log(amount=2)]
    satisfied_two, matches = evaluate(mod, pair, {"address": USDT, "min_count": 2})
    assert satisfied_two is True
    assert matches == [0, 1]

    satisfied_three, _ = evaluate(mod, pair, {"address": USDT, "min_count": 3})
    assert satisfied_three is False


def test_no_logs_satisfies_nothing(mod):
    satisfied, matches = evaluate(mod, [], {"address": USDT})
    assert satisfied is False
    assert matches == []


def test_matching_requires_the_topic_slot_to_exist(mod):
    """Asserting on topic 3 of a log that only has three topics must not read
    out of bounds, it must simply fail to match."""
    satisfied, _ = evaluate(
        mod, [erc20_transfer_log()], {"address": USDT, "topics": {"3": "0x" + word(1)}}
    )
    assert satisfied is False


def test_matched_indices_are_log_positions(mod):
    """Indices have to be positions in the receipt so a consumer can correlate
    them with the decoded event list."""
    many = [
        {"address": "0x" + "11" * 20, "topics": ["0x" + "ee" * 32], "data": "0x"},
        erc20_transfer_log(),
        {"address": "0x" + "22" * 20, "topics": ["0x" + "ee" * 32], "data": "0x"},
        erc20_transfer_log(amount=5),
    ]
    satisfied, matches = evaluate(mod, many, {"address": USDT, "min_count": 2})
    assert satisfied is True
    assert matches == [1, 3]


def test_solana_log_messages_match_on_data(mod):
    """Solana has no topics, so its program log lines are carried in ``data``
    and matched with ``data_contains``."""
    logs = [
        {"address": "", "topics": [], "data": "Program Vote111 invoke [1]"},
        {"address": "", "topics": [], "data": "Program Vote111 success"},
    ]
    satisfied, matches = evaluate(mod, logs, {"data_contains": "success"})
    assert satisfied is True
    assert matches == [1]
