"""Deterministic event log decoding.

Decoding is what turns an opaque receipt into facts an adjudicating model can
be held to. It runs entirely in code, so a model can never invent a transfer
that the logs do not contain.

The decisive case is ERC-20 against ERC-721: both emit
``Transfer(address,address,uint256)`` and therefore share a topic0. They are
told apart only by how many topics the log carries, because ERC-721 indexes
its token id and ERC-20 does not.
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


@pytest.fixture
def decode(mod):
    index = mod._build_decoder_index()

    def _decode(log: dict) -> dict:
        return mod.decode_log(log, index)

    return _decode


# -- the ERC-20 / ERC-721 distinction --------------------------------------


def test_three_topic_transfer_is_erc20(decode):
    out = decode(erc20_transfer_log())
    assert out["event"] == "ERC20Transfer"
    assert out["address"] == USDT
    assert out["fields"]["from"] == SENDER
    assert out["fields"]["to"] == RECIPIENT
    assert out["fields"]["value"] == "100000000"
    assert "token_id" not in out["fields"]


def test_four_topic_transfer_is_erc721(decode):
    out = decode(erc721_transfer_log(token_id=7804))
    assert out["event"] == "ERC721Transfer"
    assert out["fields"]["from"] == SENDER
    assert out["fields"]["to"] == RECIPIENT
    assert out["fields"]["token_id"] == "7804"
    assert "value" not in out["fields"]


def test_the_two_share_a_topic0(mod):
    """Proof that topic count is doing the disambiguation, not the hash."""
    assert erc20_transfer_log()["topics"][0] == erc721_transfer_log()["topics"][0]


def test_erc721_token_id_zero_decodes(decode):
    """Token id 0 is a real id and must not be read as missing."""
    out = decode(erc721_transfer_log(token_id=0))
    assert out["event"] == "ERC721Transfer"
    assert out["fields"]["token_id"] == "0"


def test_large_uint256_value_survives_decoding(decode):
    """No precision loss: the value is carried as a decimal string, so a
    full-width uint256 round-trips exactly."""
    huge = 2**256 - 1
    out = decode(erc20_transfer_log(amount=huge))
    assert out["fields"]["value"] == str(huge)


# -- approvals --------------------------------------------------------------


def test_erc20_approval_decodes(decode, mod):
    out = decode(
        {
            "address": USDT,
            "topics": [
                mod.topic0_for_signature("Approval(address,address,uint256)"),
                topic_pad(SENDER),
                topic_pad(RECIPIENT),
            ],
            "data": "0x" + word(500),
        }
    )
    assert out["event"] == "ERC20Approval"
    assert out["fields"]["owner"] == SENDER
    assert out["fields"]["spender"] == RECIPIENT
    assert out["fields"]["value"] == "500"


def test_erc721_approval_decodes_a_token_id(decode, mod):
    out = decode(
        {
            "address": USDT,
            "topics": [
                mod.topic0_for_signature("Approval(address,address,uint256)"),
                topic_pad(SENDER),
                topic_pad(RECIPIENT),
                "0x" + word(99),
            ],
            "data": "0x",
        }
    )
    assert out["event"] == "ERC721Approval"
    assert out["fields"]["token_id"] == "99"


def test_approval_for_all_decodes_a_bool(decode, mod):
    """bool is decoded as a real boolean, not the string "1"."""
    out = decode(
        {
            "address": USDT,
            "topics": [
                mod.topic0_for_signature("ApprovalForAll(address,address,bool)"),
                topic_pad(SENDER),
                topic_pad(RECIPIENT),
            ],
            "data": "0x" + word(1),
        }
    )
    assert out["event"] == "ERC721ApprovalForAll"
    assert out["fields"]["approved"] is True

    out_false = decode(
        {
            "address": USDT,
            "topics": [
                mod.topic0_for_signature("ApprovalForAll(address,address,bool)"),
                topic_pad(SENDER),
                topic_pad(RECIPIENT),
            ],
            "data": "0x" + word(0),
        }
    )
    assert out_false["fields"]["approved"] is False


# -- multi-token and defi --------------------------------------------------


def test_erc1155_transfer_single_decodes_both_data_words(decode, mod):
    out = decode(
        {
            "address": USDT,
            "topics": [
                mod.topic0_for_signature(
                    "TransferSingle(address,address,address,uint256,uint256)"
                ),
                topic_pad(SENDER),
                topic_pad(SENDER),
                topic_pad(RECIPIENT),
            ],
            "data": "0x" + word(12) + word(3),
        }
    )
    assert out["event"] == "ERC1155TransferSingle"
    assert out["fields"]["operator"] == SENDER
    assert out["fields"]["to"] == RECIPIENT
    assert out["fields"]["token_id"] == "12"
    assert out["fields"]["value"] == "3"


def test_governor_vote_cast_decodes(decode, mod):
    """A DAO vote, which is one of the motivating cross-chain cases."""
    out = decode(
        {
            "address": "0x408ed6354d4973f66138c91495f2f2fcbd8724c3",
            "topics": [
                mod.topic0_for_signature("VoteCast(address,uint256,uint8,uint256,string)"),
                topic_pad(SENDER),
            ],
            "data": "0x" + word(42) + word(1) + word(10**21),
        }
    )
    assert out["event"] == "GovernorVoteCast"
    assert out["fields"]["voter"] == SENDER
    assert out["fields"]["proposal_id"] == "42"
    assert out["fields"]["support"] == "1"
    assert out["fields"]["weight"] == str(10**21)


def test_aave_liquidation_decodes(decode, mod):
    """A lending liquidation, the other motivating cross-chain case."""
    out = decode(
        {
            "address": "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",
            "topics": [
                mod.topic0_for_signature(
                    "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
                ),
                topic_pad(USDT),
                topic_pad(RECIPIENT),
                topic_pad(SENDER),
            ],
            "data": "0x" + word(1000) + word(2000),
        }
    )
    assert out["event"] == "AaveLiquidationCall"
    assert out["fields"]["collateral_asset"] == USDT
    assert out["fields"]["user"] == SENDER
    assert out["fields"]["debt_to_cover"] == "1000"


def test_wrapped_native_deposit_decodes(decode, mod):
    out = decode(
        {
            "address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
            "topics": [
                mod.topic0_for_signature("Deposit(address,uint256)"),
                topic_pad(SENDER),
            ],
            "data": "0x" + word(10**18),
        }
    )
    assert out["event"] == "WrappedNativeDeposit"
    assert out["fields"]["value"] == str(10**18)


# -- graceful degradation --------------------------------------------------


def test_unrecognised_event_still_reports_structure(decode):
    """An unknown event must not be dropped. The adjudicating model needs to
    see that something happened, even unnamed, so it can answer
    'indeterminate' rather than 'mismatch'."""
    out = decode(
        {
            "address": USDT,
            "topics": ["0x" + "ee" * 32, topic_pad(SENDER)],
            "data": "0x" + word(1),
        }
    )
    assert out["event"] == "UnknownEvent"
    assert out["address"] == USDT
    assert out["topic0"] == "0x" + "ee" * 32
    assert out["topic_count"] == 2
    assert out["fields"] == {}


def test_known_signature_with_unexpected_topic_count_is_unknown(decode):
    """A Transfer topic0 with two topics matches no entry in the table. It is
    reported as unknown rather than mis-decoded as ERC-20."""
    out = decode(
        {"address": USDT, "topics": [TRANSFER_TOPIC, topic_pad(SENDER)], "data": "0x"}
    )
    assert out["event"] == "UnknownEvent"
    assert out["topic_count"] == 2


def test_anonymous_log_is_handled(decode):
    out = decode({"address": USDT, "topics": [], "data": "0x1234"})
    assert out["event"] == "AnonymousLog"
    assert out["address"] == USDT


def test_truncated_data_skips_the_missing_field(decode):
    """A log whose data is shorter than the table expects decodes what is
    present instead of raising."""
    out = decode(
        {
            "address": USDT,
            "topics": [TRANSFER_TOPIC, topic_pad(SENDER), topic_pad(RECIPIENT)],
            "data": "0x",
        }
    )
    assert out["event"] == "ERC20Transfer"
    assert out["fields"]["from"] == SENDER
    assert "value" not in out["fields"]


def test_decoding_is_case_insensitive_on_topics(decode):
    log = erc20_transfer_log()
    log["topics"] = [t.upper().replace("0X", "0x") for t in log["topics"]]
    out = decode(log)
    assert out["event"] == "ERC20Transfer"
    assert out["fields"]["from"] == SENDER


# -- the public view surface ----------------------------------------------


def test_decode_logs_view_matches_internal_decoding(verifier):
    """The exposed helper has to behave identically to the internal path, or a
    builder would validate against something other than what runs."""
    out = verifier.decode_logs(json.dumps([erc20_transfer_log(), erc721_transfer_log()]))
    assert [e["event"] for e in out["decoded"]] == ["ERC20Transfer", "ERC721Transfer"]


def test_decode_logs_view_rejects_non_json(verifier):
    with pytest.raises(Exception, match="not valid JSON"):
        verifier.decode_logs("{not json")


def test_decode_logs_view_rejects_non_array(verifier):
    with pytest.raises(Exception, match="must be a JSON array"):
        verifier.decode_logs(json.dumps({"address": USDT}))


def test_decode_logs_view_skips_non_objects(verifier):
    out = verifier.decode_logs(json.dumps([erc20_transfer_log(), "junk", 5]))
    assert len(out["decoded"]) == 1


def test_every_table_signature_has_a_distinct_decoder_key(mod):
    """Two table rows must never collide on (topic0, topic_count), otherwise
    one would silently shadow the other."""
    index = mod._build_decoder_index()
    assert len(index) == len(mod._DECODER_TABLE)
