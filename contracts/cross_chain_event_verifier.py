# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Cross-Chain Event Verifier
==========================

A GenLayer intelligent contract that answers one question trustlessly:

    "Did transaction ``tx_hash`` really happen on chain ``chain_id``,
     and did it really do what the submitter claims it did?"

Why this needs GenLayer
-----------------------
Reading another chain's history from inside a smart contract is normally
impossible. The two usual workarounds both have a cost that shows up later:

* Trust a centralised data provider. One signing key becomes the root of
  trust for every contract that depends on it.
* Run a light client or bridge. Correct, but it needs per-chain consensus
  verification, header relaying and ongoing operational work before a single
  question can be answered.

GenLayer offers a third option. Validators each fetch the transaction from
independent public sources, normalise what they get, and then have to agree.
No single validator, and no single explorer, decides the answer. The question
"did this happen" is objective, which is what makes it a good fit for an
equivalence principle: honest validators looking at honest sources converge.

What consensus is actually used for
-----------------------------------
Every verification runs inside ``gl.vm.run_nondet``. The leader performs the
whole pipeline and returns a verdict. Each validator independently performs the
*same* pipeline from scratch, against the same registry of sources, and then
compares its own verdict with the leader's. Nothing about the leader's answer is
taken on faith. The comparison is deliberately asymmetric about what must match:

* Existence and transaction identity must match exactly. These are facts
  signed by the transaction's own sender and cannot legitimately differ.
* The binary ``confirmed`` verdict must match, with a hysteresis band around
  the confirmation threshold so a boundary reading does not fail consensus.
* Whether a natural-language claim matches the transaction must match exactly.
* Confirmation counts, gas figures, explorer labels and timestamps are allowed
  to differ freely. They drift between two reads of the same transaction and
  carry no security meaning here.

That split is the whole design. It is strict where strictness is a security
property and tolerant where strictness would only produce flaky consensus.

Two verification modes
----------------------
``prove_event``  takes a structured event assertion and is fully
                 deterministic. No language model is involved at any point.
                 Use it when you know exactly which log you are looking for.
``verify_claim`` takes a natural-language description and uses a language
                 model to adjudicate it, but only ever against facts that
                 code already derived and that validators already agreed on.
                 The model cannot change whether a transaction exists or is
                 confirmed. It only answers whether the words match the facts.

``verify_transaction`` is the plain existence and confirmation check that both
of the above build on, and is the cheapest of the three.

Reading the result from another contract
----------------------------------------
    verifier = gl.get_contract_at(Address(VERIFIER_ADDRESS))
    if verifier.view().is_verified("1", tx_hash):
        ...

See ``docs/INTEGRATION.md`` for the full consumer guide.
"""

import datetime
import json
import typing
from dataclasses import dataclass

from genlayer import *


# ---------------------------------------------------------------------------
# Error classification
#
# Validators have to decide whether two failures are "the same failure".
# Prefixing every error with its class is what makes that decidable.
# ---------------------------------------------------------------------------

ERROR_EXPECTED = "[EXPECTED]"
"""Business-logic rejection. Deterministic, so both sides must produce it."""

ERROR_EXTERNAL = "[EXTERNAL]"
"""A source answered, but refused us (4xx, bad key, wrong chain). Deterministic."""

ERROR_TRANSIENT = "[TRANSIENT]"
"""Network trouble or 5xx. Non-deterministic, so agreeing that it happened is enough."""

ERROR_LLM = "[LLM_ERROR]"
"""The language model misbehaved. Never agree, force a rotation."""

_ERROR_PREFIXES = (ERROR_EXPECTED, ERROR_EXTERNAL, ERROR_TRANSIENT, ERROR_LLM)


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_ABSENT = "absent"
STATUS_DISPUTED = "disputed"

FINALITY_FINAL = "final"
FINALITY_CONFIRMING = "confirming"
FINALITY_PENDING = "pending"
FINALITY_UNKNOWN = "unknown"

AGREEMENT_STRONG = "strong"
AGREEMENT_WEAK = "weak"
AGREEMENT_NONE = "none"

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_INDETERMINATE = "indeterminate"
_VERDICTS = (VERDICT_MATCH, VERDICT_MISMATCH, VERDICT_INDETERMINATE)

KIND_EVM_JSONRPC = "evm_jsonrpc"
KIND_BLOCKSCOUT = "blockscout_v2"
KIND_ETHERSCAN = "etherscan_v2"
KIND_SOLANA_JSONRPC = "solana_jsonrpc"
_SOURCE_KINDS = (KIND_EVM_JSONRPC, KIND_BLOCKSCOUT, KIND_ETHERSCAN, KIND_SOLANA_JSONRPC)

FAMILY_EVM = "evm"
FAMILY_SOLANA = "solana"
_FAMILIES = (FAMILY_EVM, FAMILY_SOLANA)

OUTCOME_FOUND = "found"
OUTCOME_ABSENT = "absent"
OUTCOME_ERROR = "error"


# ---------------------------------------------------------------------------
# Tunables and hard safety caps
# ---------------------------------------------------------------------------

_HTTP_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    # Several public endpoints (Solana publicnode among them) answer 403 to a
    # request with no User-Agent. Verified by probing them directly.
    "User-Agent": "GenLayer-CrossChainEventVerifier/1.0",
}

MAX_LOGS_DIGESTED = 1024
"""Refuse absurd transactions rather than burn the whole compute budget."""

MAX_DECODED_EVENTS_STORED = 24
"""Logs are digested in full but only this many decoded events are persisted."""

MAX_CLAIM_CHARS = 2000
MAX_REASONING_CHARS = 700
MAX_SOURCES_HARD_CAP = 8
CONFIDENCE_TOLERANCE = 30
"""Two independent model runs may differ by this much and still agree."""


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _is_hex_string(value: str, byte_len: int) -> bool:
    if not isinstance(value, str):
        return False
    if not value.startswith("0x"):
        return False
    if len(value) != 2 + byte_len * 2:
        return False
    for ch in value[2:]:
        if ch not in "0123456789abcdefABCDEF":
            return False
    return True


def _norm_tx_hash(raw: str) -> str:
    """Normalise an EVM transaction hash, or pass a Solana signature through."""
    text = str(raw).strip()
    if text.startswith("0x") or text.startswith("0X"):
        candidate = "0x" + text[2:].lower()
        if not _is_hex_string(candidate, 32):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} malformed 32-byte transaction hash")
        return candidate
    # Solana signatures are base58 and have no 0x prefix.
    if len(text) < 32 or len(text) > 96:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} malformed transaction identifier")
    for ch in text:
        if ch not in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} malformed base58 signature")
    return text


def _norm_addr(raw: typing.Any) -> str:
    """Lowercase an address. Explorers disagree on checksum casing."""
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    if text in ("", "0x", "none", "null"):
        return ""
    return text


def _hex_to_int(raw: typing.Any, default: int = -1) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if text == "":
        return default
    try:
        if text.startswith("0x") or text.startswith("0X"):
            return int(text, 16)
        return int(text, 10)
    except ValueError:
        return default


def _dec_str(raw: typing.Any) -> str:
    """Render any integer-ish field as a canonical base-10 string."""
    value = _hex_to_int(raw, default=0)
    if value < 0:
        value = 0
    return str(value)


def _selector_of(input_hex: typing.Any) -> str:
    text = str(input_hex or "").strip().lower()
    if not text.startswith("0x") or len(text) < 10:
        return ""
    return text[:10]


def topic0_for_signature(signature: str) -> str:
    """keccak256 of a canonical event signature, which is its topic0.

    Derived at runtime rather than hardcoded, so the signature table below
    stays human-readable and auditable.
    """
    text = str(signature).strip()
    if text == "":
        raise gl.vm.UserError(f"{ERROR_EXPECTED} empty event signature")
    return "0x" + Keccak256(text.encode("utf-8")).digest().hex()


def _keccak_text(text: str) -> str:
    return "0x" + Keccak256(text.encode("utf-8")).digest().hex()


def _word_at(data_hex: str, index: int) -> int:
    """Read the 32-byte word at ``index`` out of ABI-encoded log data."""
    body = str(data_hex or "")
    if body.startswith("0x"):
        body = body[2:]
    start = index * 64
    end = start + 64
    if len(body) < end:
        return -1
    try:
        return int(body[start:end], 16)
    except ValueError:
        return -1


def _addr_from_topic(topic: str) -> str:
    text = str(topic or "")
    if text.startswith("0x"):
        text = text[2:]
    if len(text) < 40:
        return ""
    return "0x" + text[-40:].lower()


def _now_epoch_seconds() -> int:
    """Deterministic clock.

    ``gl.message_raw["datetime"]`` is the transaction's datetime, identical for
    the leader and every validator, which is exactly what a consensus-safe
    clock has to be. Verified live on studionet as ``2026-10-10T02:19:40.615407Z``.
    """
    raw = str(gl.message_raw["datetime"]).strip()
    text = raw.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} unparseable transaction datetime {raw}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def _iso_from_epoch(seconds: int) -> str:
    moment = datetime.datetime.fromtimestamp(int(seconds), tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str, limit: int) -> str:
    body = str(text or "")
    if len(body) <= limit:
        return body
    return body[:limit]


def _record_key(chain_id: str, tx_hash: str) -> str:
    return f"{chain_id}|{tx_hash}"


def _sub_key(chain_id: str, tx_hash: str, payload: str) -> str:
    return f"{chain_id}|{tx_hash}|{_keccak_text(payload)}"


# ---------------------------------------------------------------------------
# Event decoding
#
# A log is decoded by looking up (signature, number_of_topics). The topic count
# is what distinguishes an ERC-20 Transfer (two indexed fields) from an
# ERC-721 Transfer (three), which a signature alone cannot do.
# ---------------------------------------------------------------------------

_DECODER_TABLE: tuple[tuple[str, int, str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]], ...] = (
    # signature, topic_count, label, indexed fields, data fields
    (
        "Transfer(address,address,uint256)",
        3,
        "ERC20Transfer",
        (("from", "address"), ("to", "address")),
        (("value", "uint256"),),
    ),
    (
        "Transfer(address,address,uint256)",
        4,
        "ERC721Transfer",
        (("from", "address"), ("to", "address"), ("token_id", "uint256")),
        (),
    ),
    (
        "Approval(address,address,uint256)",
        3,
        "ERC20Approval",
        (("owner", "address"), ("spender", "address")),
        (("value", "uint256"),),
    ),
    (
        "Approval(address,address,uint256)",
        4,
        "ERC721Approval",
        (("owner", "address"), ("approved", "address"), ("token_id", "uint256")),
        (),
    ),
    (
        "ApprovalForAll(address,address,bool)",
        3,
        "ERC721ApprovalForAll",
        (("owner", "address"), ("operator", "address")),
        (("approved", "bool"),),
    ),
    (
        "TransferSingle(address,address,address,uint256,uint256)",
        4,
        "ERC1155TransferSingle",
        (("operator", "address"), ("from", "address"), ("to", "address")),
        (("token_id", "uint256"), ("value", "uint256")),
    ),
    (
        "TransferBatch(address,address,address,uint256[],uint256[])",
        4,
        "ERC1155TransferBatch",
        (("operator", "address"), ("from", "address"), ("to", "address")),
        (),
    ),
    (
        "Deposit(address,uint256)",
        2,
        "WrappedNativeDeposit",
        (("owner", "address"),),
        (("value", "uint256"),),
    ),
    (
        "Withdrawal(address,uint256)",
        2,
        "WrappedNativeWithdrawal",
        (("owner", "address"),),
        (("value", "uint256"),),
    ),
    (
        "OwnershipTransferred(address,address)",
        3,
        "OwnershipTransferred",
        (("previous_owner", "address"), ("new_owner", "address")),
        (),
    ),
    (
        "Swap(address,uint256,uint256,uint256,uint256,address)",
        3,
        "UniswapV2Swap",
        (("sender", "address"), ("to", "address")),
        (
            ("amount0_in", "uint256"),
            ("amount1_in", "uint256"),
            ("amount0_out", "uint256"),
            ("amount1_out", "uint256"),
        ),
    ),
    (
        "Swap(address,address,int256,int256,uint160,uint128,int24)",
        3,
        "UniswapV3Swap",
        (("sender", "address"), ("recipient", "address")),
        (),
    ),
    (
        "VoteCast(address,uint256,uint8,uint256,string)",
        2,
        "GovernorVoteCast",
        (("voter", "address"),),
        (("proposal_id", "uint256"), ("support", "uint256"), ("weight", "uint256")),
    ),
    (
        "ProposalExecuted(uint256)",
        1,
        "GovernorProposalExecuted",
        (),
        (("proposal_id", "uint256"),),
    ),
    (
        "ProposalQueued(uint256,uint256)",
        1,
        "GovernorProposalQueued",
        (),
        (("proposal_id", "uint256"), ("eta", "uint256")),
    ),
    (
        "LiquidationCall(address,address,address,uint256,uint256,address,bool)",
        4,
        "AaveLiquidationCall",
        (
            ("collateral_asset", "address"),
            ("debt_asset", "address"),
            ("user", "address"),
        ),
        (("debt_to_cover", "uint256"), ("liquidated_collateral_amount", "uint256")),
    ),
    (
        "Borrow(address,address,address,uint256,uint8,uint256,uint16)",
        3,
        "AaveBorrow",
        (("reserve", "address"), ("on_behalf_of", "address")),
        (),
    ),
    (
        "Repay(address,address,address,uint256,bool)",
        4,
        "AaveRepay",
        (("reserve", "address"), ("user", "address"), ("repayer", "address")),
        (("amount", "uint256"),),
    ),
    (
        "Supply(address,address,address,uint256,uint16)",
        4,
        "AaveSupply",
        (("reserve", "address"), ("on_behalf_of", "address"), ("referral", "uint256")),
        (),
    ),
    (
        "Withdraw(address,address,address,uint256)",
        4,
        "AaveWithdraw",
        (("reserve", "address"), ("user", "address"), ("to", "address")),
        (("amount", "uint256"),),
    ),
)


def _build_decoder_index() -> dict[str, tuple]:
    """Map "topic0|topic_count" to a decoder spec. Built once per execution."""
    index: dict[str, tuple] = {}
    for signature, topic_count, label, indexed, data_fields in _DECODER_TABLE:
        key = topic0_for_signature(signature) + "|" + str(topic_count)
        index[key] = (signature, label, indexed, data_fields)
    return index


def _decode_scalar(kind: str, raw_value: int) -> typing.Any:
    if kind == "bool":
        return raw_value != 0
    return str(raw_value)


def decode_log(log: dict, decoder_index: dict[str, tuple]) -> dict:
    """Turn one raw log into a structured, named event where we recognise it.

    Unrecognised logs still come back with their address, topic0 and topic
    count, so an adjudicating model always sees something structured rather
    than an opaque blob.
    """
    topics = [str(t).lower() for t in (log.get("topics") or []) if t]
    address = _norm_addr(log.get("address"))
    data = str(log.get("data") or "0x")

    if not topics:
        return {
            "event": "AnonymousLog",
            "signature": "",
            "address": address,
            "topic_count": 0,
            "fields": {},
        }

    spec = decoder_index.get(topics[0] + "|" + str(len(topics)))
    if spec is None:
        return {
            "event": "UnknownEvent",
            "signature": "",
            "address": address,
            "topic0": topics[0],
            "topic_count": len(topics),
            "fields": {},
        }

    signature, label, indexed, data_fields = spec
    fields: dict[str, typing.Any] = {}
    for offset, (name, kind) in enumerate(indexed):
        topic = topics[offset + 1] if offset + 1 < len(topics) else ""
        if kind == "address":
            fields[name] = _addr_from_topic(topic)
        else:
            fields[name] = _decode_scalar(kind, _hex_to_int(topic, default=0))
    for offset, (name, kind) in enumerate(data_fields):
        word = _word_at(data, offset)
        if word < 0:
            continue
        if kind == "address":
            fields[name] = "0x" + format(word, "040x")
        else:
            fields[name] = _decode_scalar(kind, word)

    return {
        "event": label,
        "signature": signature,
        "address": address,
        "topic_count": len(topics),
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# Structured event assertions (the deterministic verification mode)
# ---------------------------------------------------------------------------


def parse_assertion(raw: str) -> dict:
    """Validate and canonicalise an event assertion.

    Accepted shape, every key optional except that at least one matching
    criterion has to be present::

        {
          "address":       "0x...",     log emitter
          "signature":     "Transfer(address,address,uint256)",
          "topic0":        "0x...",     explicit alternative to signature
          "topics":        {"1": "0x...", "2": "0x..."},
          "data_contains": "0x...",     hex fragment that must appear in data
          "min_count":     1
        }
    """
    try:
        spec = json.loads(str(raw))
    except (ValueError, TypeError):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} assertion is not valid JSON")
    if not isinstance(spec, dict):
        raise gl.vm.UserError(f"{ERROR_EXPECTED} assertion must be a JSON object")

    out: dict[str, typing.Any] = {}

    address = _norm_addr(spec.get("address"))
    if address:
        if not _is_hex_string(address, 20):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} assertion address is not 20 bytes")
        out["address"] = address

    topic0 = _norm_addr(spec.get("topic0")) if spec.get("topic0") else ""
    signature = str(spec.get("signature") or "").strip()
    if signature:
        derived = topic0_for_signature(signature)
        if topic0 and topic0 != derived:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} assertion topic0 contradicts its signature"
            )
        topic0 = derived
        out["signature"] = signature
    if topic0:
        if not _is_hex_string(topic0, 32):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} assertion topic0 is not 32 bytes")
        out["topic0"] = topic0

    raw_topics = spec.get("topics")
    if raw_topics is not None:
        if not isinstance(raw_topics, dict):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} assertion topics must be an object")
        topics: dict[str, str] = {}
        for position, expected in raw_topics.items():
            try:
                slot = int(str(position))
            except ValueError:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} assertion topic position {position} is not an index"
                )
            if slot < 1 or slot > 3:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} assertion topic position must be 1..3, got {slot}"
                )
            value = _norm_addr(expected)
            if _is_hex_string(value, 20):
                # Accept a plain address and left-pad it to a topic word.
                value = "0x" + ("0" * 24) + value[2:]
            if not _is_hex_string(value, 32):
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} assertion topic {slot} must be 20 or 32 bytes"
                )
            topics[str(slot)] = value
        if topics:
            out["topics"] = topics

    fragment = spec.get("data_contains")
    if fragment is not None:
        text = str(fragment).strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        if text == "":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} assertion data_contains is empty")
        out["data_contains"] = text

    min_count = _hex_to_int(spec.get("min_count"), default=1)
    if min_count < 1:
        min_count = 1
    out["min_count"] = min_count

    if not any(k in out for k in ("address", "topic0", "topics", "data_contains")):
        raise gl.vm.UserError(
            f"{ERROR_EXPECTED} assertion needs at least one of address, signature, "
            "topic0, topics or data_contains"
        )
    return out


def canonical_assertion(spec: dict) -> str:
    """Stable text form, so the same assertion always hashes to the same key."""
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def _log_matches(log: dict, spec: dict) -> bool:
    topics = [str(t).lower() for t in (log.get("topics") or []) if t]

    want_address = spec.get("address")
    if want_address and _norm_addr(log.get("address")) != want_address:
        return False

    want_topic0 = spec.get("topic0")
    if want_topic0:
        if not topics or topics[0] != want_topic0:
            return False

    want_topics = spec.get("topics") or {}
    for position, expected in want_topics.items():
        slot = int(position)
        if slot >= len(topics):
            return False
        if topics[slot] != str(expected).lower():
            return False

    fragment = spec.get("data_contains")
    if fragment:
        body = str(log.get("data") or "").lower()
        if body.startswith("0x"):
            body = body[2:]
        if fragment not in body:
            return False

    return True


def evaluate_assertion(logs: list, spec: dict) -> tuple[bool, list]:
    """Run an assertion against every log. Pure, so it needs no consensus."""
    matches: list[int] = []
    for index, log in enumerate(logs):
        if _log_matches(log, spec):
            matches.append(index)
    return (len(matches) >= int(spec.get("min_count", 1)), matches)


# ---------------------------------------------------------------------------
# Canonical fact digests
#
# Two digests rather than one, because the two halves of a transaction have
# different lifetimes:
#
#   identity   is signed by the sender and fixed the moment it is broadcast
#   settlement only exists once the transaction is mined
#
# Comparing them separately is what lets a pending-versus-mined race between
# the leader and a validator pass consensus, without ever loosening the
# guarantee on a transaction that both sides already see as mined.
# ---------------------------------------------------------------------------


def identity_digest(facts: dict) -> str:
    canonical = "\n".join(
        [
            "v1",
            str(facts.get("from", "")),
            str(facts.get("to", "")),
            str(facts.get("value", "0")),
            str(facts.get("selector", "")),
            str(facts.get("nonce", -1)),
        ]
    )
    return _keccak_text(canonical)


def settlement_digest(facts: dict) -> str:
    logs = facts.get("logs") or []
    parts = [
        "v1",
        str(facts.get("status", "")),
        str(facts.get("block_number", -1)),
        str(facts.get("tx_index", -1)),
        str(facts.get("created_contract", "")),
        str(len(logs)),
    ]
    for log in logs:
        topics = ",".join(str(t).lower() for t in (log.get("topics") or []) if t)
        parts.append(
            f"{_norm_addr(log.get('address'))}#{topics}#{str(log.get('data') or '').lower()}"
        )
    return _keccak_text("\n".join(parts))


# ---------------------------------------------------------------------------
# Source adapters
#
# Each adapter turns one explorer's response into the same normalised shape:
#
#   {"outcome": found|absent|error, "note": str, "facts": {...},
#    "head": int, "confirmations": int}
#
# Normalising before comparing is what makes two explorers that disagree about
# formatting still agree about facts.
# ---------------------------------------------------------------------------


def _http_json(method: str, url: str, body: bytes | None) -> typing.Any:
    """One HTTP round trip, with status codes mapped onto the error classes."""
    try:
        if method == "POST":
            response = gl.nondet.web.post(url, body=body, headers=_HTTP_HEADERS)
        else:
            response = gl.nondet.web.get(url, headers=_HTTP_HEADERS)
    except Exception as exc:  # noqa: BLE001 - classify, never leak a raw error
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} request failed: {type(exc).__name__}")

    status = int(response.status)
    if status == 429:
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} source rate limited (429)")
    if 400 <= status < 500:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} source rejected request ({status})")
    if status >= 500:
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} source unavailable ({status})")

    raw = response.body or b""
    if not raw:
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} source returned an empty body")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise gl.vm.UserError(f"{ERROR_TRANSIENT} source returned malformed JSON")


def _evm_facts_from_rpc(tx: dict, receipt: dict | None) -> dict:
    """Normalise eth_getTransactionByHash plus eth_getTransactionReceipt."""
    block_number = _hex_to_int(tx.get("blockNumber"), default=-1)
    mined = block_number >= 0

    logs: list[dict] = []
    status = STATUS_PENDING
    created = ""
    tx_index = -1

    if receipt:
        tx_index = _hex_to_int(receipt.get("transactionIndex"), default=-1)
        created = _norm_addr(receipt.get("contractAddress"))
        raw_status = receipt.get("status")
        if raw_status is None:
            # Pre-Byzantium receipts have no status field. Treat the presence of
            # a receipt as inclusion and leave success undetermined.
            status = STATUS_SUCCESS
        else:
            status = STATUS_SUCCESS if _hex_to_int(raw_status, 0) == 1 else STATUS_FAILED
        for entry in receipt.get("logs") or []:
            logs.append(
                {
                    "address": _norm_addr(entry.get("address")),
                    "topics": [str(t).lower() for t in (entry.get("topics") or []) if t],
                    "data": str(entry.get("data") or "0x").lower(),
                }
            )
    elif mined:
        # Mined according to the transaction object, but this backend has no
        # receipt for it. Observed repeatedly on load-balanced public RPC where
        # some backends are pruned. Report inclusion without settlement rather
        # than inventing either.
        status = STATUS_PENDING

    if len(logs) > MAX_LOGS_DIGESTED:
        raise gl.vm.UserError(
            f"{ERROR_EXPECTED} transaction has {len(logs)} logs, above the "
            f"{MAX_LOGS_DIGESTED} cap"
        )

    return {
        "status": status,
        "from": _norm_addr(tx.get("from")),
        "to": _norm_addr(tx.get("to")),
        "value": _dec_str(tx.get("value")),
        "nonce": _hex_to_int(tx.get("nonce"), default=-1),
        "block_number": block_number,
        "tx_index": tx_index,
        "selector": _selector_of(tx.get("input")),
        "created_contract": created,
        "logs": logs,
        "accounts": [],
        "programs": [],
    }


def fetch_evm_jsonrpc(source: dict, plan: dict, tx_hash: str) -> dict:
    """Standard EVM node. One batched POST keeps us inside the time budget.

    ``eth_chainId`` travels in the same batch and is checked against the
    registry, so a misconfigured or swapped endpoint cannot quietly serve a
    transaction from a different chain.
    """
    payload = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_getTransactionByHash",
                "params": [tx_hash],
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "eth_getTransactionReceipt",
                "params": [tx_hash],
            },
            {"jsonrpc": "2.0", "id": 4, "method": "eth_blockNumber", "params": []},
        ]
    ).encode("utf-8")

    parsed = _http_json("POST", source["url"], payload)
    if not isinstance(parsed, list):
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} source does not support JSON-RPC batch")

    by_id: dict[int, typing.Any] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if "error" in item and item.get("error"):
            detail = item.get("error")
            message = ""
            if isinstance(detail, dict):
                message = str(detail.get("message", ""))
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} rpc error: {_clip(message, 90)}")
        by_id[_hex_to_int(item.get("id"), default=-1)] = item.get("result")

    expected_chain = int(plan.get("expected_evm_chain_id", -1))
    if expected_chain >= 0:
        reported = _hex_to_int(by_id.get(1), default=-1)
        if reported >= 0 and reported != expected_chain:
            raise gl.vm.UserError(
                f"{ERROR_EXTERNAL} source serves chain {reported}, expected {expected_chain}"
            )

    head = _hex_to_int(by_id.get(4), default=-1)
    tx = by_id.get(2)
    if not isinstance(tx, dict):
        return {"outcome": OUTCOME_ABSENT, "note": "no transaction", "head": head}

    receipt = by_id.get(3)
    facts = _evm_facts_from_rpc(tx, receipt if isinstance(receipt, dict) else None)
    confirmations = 0
    if facts["block_number"] >= 0 and head >= facts["block_number"]:
        confirmations = head - facts["block_number"] + 1
    return {
        "outcome": OUTCOME_FOUND,
        "note": "receipt" if isinstance(receipt, dict) else "no receipt on this backend",
        "facts": facts,
        "head": head,
        "confirmations": confirmations,
    }


def fetch_blockscout_v2(source: dict, plan: dict, tx_hash: str) -> dict:
    """Blockscout's v2 REST API.

    Blockscout is an indexer with a database rather than a node, so it does not
    suffer the pruned-receipt problem that public RPC does. It also reports
    ``confirmations`` directly, which is exactly the field that is allowed to
    differ between two reads.
    """
    base = str(source["url"]).rstrip("/")
    detail = _http_json("GET", f"{base}/api/v2/transactions/{tx_hash}", None)
    if not isinstance(detail, dict):
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} unexpected blockscout payload")

    message = str(detail.get("message", "")).lower()
    if "not found" in message or detail.get("hash") is None:
        return {"outcome": OUTCOME_ABSENT, "note": "not indexed", "head": -1}

    block_number = _hex_to_int(detail.get("block_number"), default=-1)
    confirmations = _hex_to_int(detail.get("confirmations"), default=0)
    if confirmations < 0:
        confirmations = 0

    result_text = str(detail.get("result", "")).lower()
    status_text = str(detail.get("status", "")).lower()
    if block_number < 0:
        status = STATUS_PENDING
    elif status_text == "ok" or result_text == "success":
        status = STATUS_SUCCESS
    elif status_text == "error" or result_text not in ("", "pending"):
        status = STATUS_FAILED
    else:
        status = STATUS_PENDING

    logs: list[dict] = []
    if block_number >= 0:
        log_page = _http_json("GET", f"{base}/api/v2/transactions/{tx_hash}/logs", None)
        items = []
        if isinstance(log_page, dict):
            items = log_page.get("items") or []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            address_field = entry.get("address")
            address = (
                address_field.get("hash")
                if isinstance(address_field, dict)
                else address_field
            )
            logs.append(
                {
                    "address": _norm_addr(address),
                    # Blockscout pads the topics array with nulls up to 4.
                    "topics": [str(t).lower() for t in (entry.get("topics") or []) if t],
                    "data": str(entry.get("data") or "0x").lower(),
                }
            )
        logs.sort(key=lambda item: (item["address"], ",".join(item["topics"]), item["data"]))

    if len(logs) > MAX_LOGS_DIGESTED:
        raise gl.vm.UserError(
            f"{ERROR_EXPECTED} transaction has {len(logs)} logs, above the "
            f"{MAX_LOGS_DIGESTED} cap"
        )

    from_field = detail.get("from") or {}
    to_field = detail.get("to") or {}
    created_field = detail.get("created_contract") or {}
    facts = {
        "status": status,
        "from": _norm_addr(
            from_field.get("hash") if isinstance(from_field, dict) else from_field
        ),
        "to": _norm_addr(to_field.get("hash") if isinstance(to_field, dict) else to_field),
        "value": _dec_str(detail.get("value")),
        "nonce": _hex_to_int(detail.get("nonce"), default=-1),
        "block_number": block_number,
        "tx_index": _hex_to_int(detail.get("position"), default=-1),
        "selector": _selector_of(detail.get("raw_input")),
        "created_contract": _norm_addr(
            created_field.get("hash")
            if isinstance(created_field, dict)
            else created_field
        ),
        "logs": logs,
        "accounts": [],
        "programs": [],
    }
    head = block_number + confirmations if block_number >= 0 else -1
    return {
        "outcome": OUTCOME_FOUND,
        "note": "indexed",
        "facts": facts,
        "head": head,
        "confirmations": confirmations,
    }


def fetch_etherscan_v2(source: dict, plan: dict, tx_hash: str) -> dict:
    """Etherscan's unified v2 multichain API. Requires an API key.

    Not a default source because the keyless endpoint answers
    ``Missing/Invalid API Key``. Register it with ``add_source`` and a key.
    """
    base = str(source["url"]).rstrip("/")
    api_key = str(source.get("api_key") or "")
    if not api_key:
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} etherscan source has no api key")
    chain_param = int(plan.get("expected_evm_chain_id", -1))
    if chain_param < 0:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} etherscan source needs a numeric chain id")
    prefix = f"{base}?chainid={chain_param}&module=proxy"

    def proxy(action: str, extra: str) -> typing.Any:
        payload = _http_json("GET", f"{prefix}&action={action}{extra}&apikey={api_key}", None)
        if isinstance(payload, dict):
            if str(payload.get("message", "")).upper() == "NOTOK":
                raise gl.vm.UserError(
                    f"{ERROR_EXTERNAL} etherscan refused: {_clip(str(payload.get('result')), 80)}"
                )
            if payload.get("error"):
                detail = payload.get("error")
                message = detail.get("message") if isinstance(detail, dict) else detail
                raise gl.vm.UserError(
                    f"{ERROR_EXTERNAL} etherscan error: {_clip(str(message), 80)}"
                )
            return payload.get("result")
        return None

    tx = proxy("eth_getTransactionByHash", f"&txhash={tx_hash}")
    head = _hex_to_int(proxy("eth_blockNumber", ""), default=-1)
    if not isinstance(tx, dict):
        return {"outcome": OUTCOME_ABSENT, "note": "no transaction", "head": head}

    receipt = proxy("eth_getTransactionReceipt", f"&txhash={tx_hash}")
    facts = _evm_facts_from_rpc(tx, receipt if isinstance(receipt, dict) else None)
    confirmations = 0
    if facts["block_number"] >= 0 and head >= facts["block_number"]:
        confirmations = head - facts["block_number"] + 1
    return {
        "outcome": OUTCOME_FOUND,
        "note": "etherscan proxy",
        "facts": facts,
        "head": head,
        "confirmations": confirmations,
    }


def fetch_solana_jsonrpc(source: dict, plan: dict, tx_hash: str) -> dict:
    """Solana JSON-RPC.

    Solana has no EVM-style topics, so the normalised ``logs`` list carries one
    entry per program log message with an empty ``topics`` array. Structured
    assertions therefore match Solana activity through ``data_contains`` on
    those messages. ``accounts`` and ``programs`` are reported separately.

    The request asks for ``finalized`` commitment, so a transaction that comes
    back at all has already been finalised by the cluster.
    """
    payload = json.dumps(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    tx_hash,
                    {
                        "encoding": "json",
                        "maxSupportedTransactionVersion": 0,
                        "commitment": "finalized",
                    },
                ],
            },
            {"jsonrpc": "2.0", "id": 2, "method": "getSlot", "params": []},
        ]
    ).encode("utf-8")

    parsed = _http_json("POST", source["url"], payload)
    if not isinstance(parsed, list):
        raise gl.vm.UserError(f"{ERROR_EXTERNAL} solana source does not support batch")

    by_id: dict[int, typing.Any] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            detail = item.get("error")
            message = detail.get("message") if isinstance(detail, dict) else detail
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} solana rpc error: {_clip(str(message), 90)}")
        by_id[_hex_to_int(item.get("id"), default=-1)] = item.get("result")

    head = _hex_to_int(by_id.get(2), default=-1)
    tx = by_id.get(1)
    if not isinstance(tx, dict):
        return {"outcome": OUTCOME_ABSENT, "note": "no finalized transaction", "head": head}

    meta = tx.get("meta") or {}
    slot = _hex_to_int(tx.get("slot"), default=-1)
    inner = tx.get("transaction") or {}
    message = inner.get("message") or {}
    account_keys = [str(k) for k in (message.get("accountKeys") or [])]

    programs: list[str] = []
    for instruction in message.get("instructions") or []:
        if not isinstance(instruction, dict):
            continue
        index = _hex_to_int(instruction.get("programIdIndex"), default=-1)
        if 0 <= index < len(account_keys):
            program = account_keys[index]
            if program not in programs:
                programs.append(program)

    logs: list[dict] = []
    for line in meta.get("logMessages") or []:
        logs.append({"address": "", "topics": [], "data": str(line)})
    if len(logs) > MAX_LOGS_DIGESTED:
        raise gl.vm.UserError(
            f"{ERROR_EXPECTED} transaction has {len(logs)} log messages, above the "
            f"{MAX_LOGS_DIGESTED} cap"
        )

    facts = {
        "status": STATUS_FAILED if meta.get("err") is not None else STATUS_SUCCESS,
        "from": account_keys[0] if account_keys else "",
        "to": "",
        "value": "0",
        "nonce": -1,
        "block_number": slot,
        "tx_index": _hex_to_int(tx.get("transactionIndex"), default=-1),
        "selector": "",
        "created_contract": "",
        "logs": logs,
        "accounts": account_keys,
        "programs": programs,
    }
    confirmations = 0
    if slot >= 0 and head >= slot:
        confirmations = head - slot + 1
    return {
        "outcome": OUTCOME_FOUND,
        "note": "finalized",
        "facts": facts,
        "head": head,
        "confirmations": confirmations,
    }


def observe_source(source: dict, plan: dict, tx_hash: str) -> dict:
    """Query one source and never let its failure abort the whole verification.

    Dispatch is written out explicitly rather than looked up in a table so the
    path from the consensus entry point down to each HTTP call stays statically
    traceable, both for the GenVM linter and for anyone auditing the contract.
    """
    kind = str(source.get("kind"))
    label = str(source.get("label", "?"))
    try:
        if kind == KIND_EVM_JSONRPC:
            observation = fetch_evm_jsonrpc(source, plan, tx_hash)
        elif kind == KIND_BLOCKSCOUT:
            observation = fetch_blockscout_v2(source, plan, tx_hash)
        elif kind == KIND_ETHERSCAN:
            observation = fetch_etherscan_v2(source, plan, tx_hash)
        elif kind == KIND_SOLANA_JSONRPC:
            observation = fetch_solana_jsonrpc(source, plan, tx_hash)
        else:
            return {
                "label": label,
                "kind": kind,
                "outcome": OUTCOME_ERROR,
                "note": f"unsupported source kind {kind!r}",
            }
    except gl.vm.UserError as err:
        message = err.message if hasattr(err, "message") else str(err)
        if message.startswith(ERROR_EXPECTED):
            # A cap breach is a property of the transaction itself, not of this
            # source, so it has to stop the whole verification rather than be
            # recorded as one source having a bad day.
            raise
        return {
            "label": label,
            "kind": kind,
            "outcome": OUTCOME_ERROR,
            "note": _clip(message, 120),
        }
    observation["label"] = label
    observation["kind"] = kind
    return observation


# ---------------------------------------------------------------------------
# Aggregation across sources
# ---------------------------------------------------------------------------


def derive_finality(
    exists: bool, mined: bool, confirmations: int, min_confirmations: int
) -> str:
    if not exists:
        return FINALITY_UNKNOWN
    if not mined:
        return FINALITY_PENDING
    if confirmations >= min_confirmations:
        return FINALITY_FINAL
    return FINALITY_CONFIRMING


def aggregate(observations: list, plan: dict) -> dict:
    """Reduce per-source observations to one verdict.

    Presence and absence are deliberately not symmetric. A node cannot
    fabricate a signed transaction object, but it can easily fail to hold one:
    public RPC endpoints were observed returning ``null`` receipts for a
    transaction that demonstrably exists, because they load-balance across
    partly pruned backends. So positive evidence carries, while declaring a
    transaction absent requires a quorum of sources that all agree it is.
    """
    quorum = int(plan["quorum"])
    min_confirmations = int(plan["min_confirmations"])
    drift = int(plan["drift_tolerance"])

    positives = [o for o in observations if o.get("outcome") == OUTCOME_FOUND]
    absents = [o for o in observations if o.get("outcome") == OUTCOME_ABSENT]
    errors = [o for o in observations if o.get("outcome") == OUTCOME_ERROR]

    reports = [
        {
            "label": str(o.get("label", "?")),
            "kind": str(o.get("kind", "?")),
            "outcome": str(o.get("outcome", "?")),
            "note": _clip(str(o.get("note", "")), 120),
            "confirmations": int(o.get("confirmations", 0) or 0),
        }
        for o in observations
    ]

    base = {
        "sources_queried": len(observations),
        "sources_positive": len(positives),
        "sources_absent": len(absents),
        "sources_errored": len(errors),
        "sources_agreeing": 0,
        "source_reports": reports,
    }

    if not positives and not absents:
        raise gl.vm.UserError(
            f"{ERROR_TRANSIENT} every source failed ({len(errors)} of {len(observations)})"
        )

    if not positives:
        if len(absents) < quorum:
            raise gl.vm.UserError(
                f"{ERROR_TRANSIENT} only {len(absents)} source(s) reachable, "
                f"{quorum} needed to declare a transaction absent"
            )
        base.update(
            {
                "exists": False,
                "mined": False,
                "confirmed": False,
                "status": STATUS_ABSENT,
                "finality": FINALITY_UNKNOWN,
                "near_threshold": False,
                "agreement": AGREEMENT_STRONG,
                "sources_agreeing": len(absents),
                "identity_digest": "",
                "settlement_digest": "",
                "from": "",
                "to": "",
                "value": "0",
                "nonce": -1,
                "block_number": -1,
                "tx_index": -1,
                "selector": "",
                "created_contract": "",
                "log_count": 0,
                "confirmations": 0,
                "head": -1,
                "accounts": [],
                "programs": [],
                "logs": [],
            }
        )
        return base

    # Group positive observations by the identity they report. Identity is
    # signed by the sender, so two honest sources cannot disagree about it.
    groups: dict[str, list] = {}
    for observation in positives:
        key = identity_digest(observation["facts"])
        groups.setdefault(key, []).append(observation)

    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    winner_key, winner = ranked[0]
    if len(ranked) > 1 and len(ranked[1][1]) == len(winner):
        base.update(
            {
                "exists": True,
                "mined": False,
                "confirmed": False,
                "status": STATUS_DISPUTED,
                "finality": FINALITY_UNKNOWN,
                "near_threshold": False,
                "agreement": AGREEMENT_NONE,
                "sources_agreeing": len(winner),
                "identity_digest": "",
                "settlement_digest": "",
                "from": "",
                "to": "",
                "value": "0",
                "nonce": -1,
                "block_number": -1,
                "tx_index": -1,
                "selector": "",
                "created_contract": "",
                "log_count": 0,
                "confirmations": 0,
                "head": -1,
                "accounts": [],
                "programs": [],
                "logs": [],
            }
        )
        return base

    # Prefer a member that actually has settlement data. On a pruned backend a
    # transaction can be visible without its receipt.
    mined_members = [o for o in winner if o["facts"]["block_number"] >= 0]
    settled = [o for o in mined_members if o["facts"]["status"] != STATUS_PENDING]
    pool = settled or mined_members or winner
    representative = max(pool, key=lambda o: int(o.get("confirmations", 0) or 0))
    facts = representative["facts"]

    # Settlement has to be corroborated on its own. Sources that report the
    # same identity may still differ on logs if one of them is mid-reindex.
    settlement_groups: dict[str, int] = {}
    for observation in pool:
        settlement_groups[settlement_digest(observation["facts"])] = (
            settlement_groups.get(settlement_digest(observation["facts"]), 0) + 1
        )
    chosen_settlement = settlement_digest(facts)
    settlement_votes = settlement_groups.get(chosen_settlement, 0)

    mined = facts["block_number"] >= 0 and facts["status"] != STATUS_PENDING
    confirmations = int(representative.get("confirmations", 0) or 0)
    finality = derive_finality(True, mined, confirmations, min_confirmations)

    identity_votes = len(winner)
    strong_identity = identity_votes >= quorum
    strong_settlement = settlement_votes >= quorum or len(pool) == 1 and identity_votes >= quorum
    # A single corroborating source is never enough to call something confirmed,
    # no matter how deep its confirmation count looks.
    agreement = AGREEMENT_STRONG if strong_identity else AGREEMENT_WEAK

    confirmed = (
        agreement == AGREEMENT_STRONG
        and mined
        and facts["status"] == STATUS_SUCCESS
        and finality == FINALITY_FINAL
        and settlement_votes >= min(quorum, len(pool))
    )
    near_threshold = mined and abs(confirmations - min_confirmations) <= drift

    base.update(
        {
            "exists": True,
            "mined": mined,
            "confirmed": confirmed,
            "status": facts["status"],
            "finality": finality,
            "near_threshold": near_threshold,
            "agreement": agreement,
            "sources_agreeing": identity_votes,
            "sources_settlement_agreeing": settlement_votes,
            "identity_digest": winner_key,
            "settlement_digest": chosen_settlement,
            "from": facts["from"],
            "to": facts["to"],
            "value": facts["value"],
            "nonce": facts["nonce"],
            "block_number": facts["block_number"],
            "tx_index": facts["tx_index"],
            "selector": facts["selector"],
            "created_contract": facts["created_contract"],
            "log_count": len(facts["logs"]),
            "confirmations": confirmations,
            "head": int(representative.get("head", -1) or -1),
            "accounts": facts.get("accounts") or [],
            "programs": facts.get("programs") or [],
            "logs": facts["logs"],
        }
    )
    return base


def gather(plan: dict, tx_hash: str) -> dict:
    """Query sources in registry order, stopping once a quorum corroborates.

    Stopping early is safe for consensus because the comparison that follows
    is over the derived verdict, not over which sources happened to answer.
    """
    quorum = int(plan["quorum"])
    budget = int(plan["max_sources"])
    observations: list = []

    for source in plan["sources"][:budget]:
        observations.append(observe_source(source, plan, tx_hash))

        positives = [o for o in observations if o.get("outcome") == OUTCOME_FOUND]
        if len(positives) >= quorum:
            groups: dict[str, int] = {}
            for observation in positives:
                key = identity_digest(observation["facts"])
                groups[key] = groups.get(key, 0) + 1
            best = max(groups.values())
            settled = [
                o
                for o in positives
                if o["facts"]["block_number"] >= 0
                and o["facts"]["status"] != STATUS_PENDING
            ]
            if best >= quorum and len(settled) >= quorum:
                break

    return aggregate(observations, plan)


# ---------------------------------------------------------------------------
# Claim adjudication
# ---------------------------------------------------------------------------


def render_ground_truth(result: dict, decoded: list) -> str:
    """Facts that code already derived, laid out for the adjudicating model.

    Everything here came from source data that validators already agreed on.
    The model is asked to compare words against these lines, never to supply
    or override them.
    """
    lines = [
        f"chain_id: {result.get('chain_id', '')}",
        f"transaction_hash: {result.get('tx_hash', '')}",
        f"exists: {result['exists']}",
        f"execution_status: {result['status']}",
        f"confirmed_by_consensus: {result['confirmed']}",
        f"finality: {result['finality']} ({result['confirmations']} confirmations)",
        f"block_number: {result['block_number']}",
        f"sender: {result['from'] or '(none)'}",
        f"recipient: {result['to'] or '(none, contract creation or program call)'}",
        f"native_value_wei: {result['value']}",
        f"method_selector: {result['selector'] or '(none, plain transfer)'}",
        f"created_contract: {result['created_contract'] or '(none)'}",
        f"log_count: {result['log_count']}",
        f"corroborating_sources: {result['sources_agreeing']} of {result['sources_queried']}",
    ]
    if result.get("accounts"):
        lines.append("accounts: " + ", ".join(result["accounts"][:12]))
    if result.get("programs"):
        lines.append("programs: " + ", ".join(result["programs"][:8]))

    if decoded:
        lines.append("decoded_events:")
        for index, event in enumerate(decoded[:MAX_DECODED_EVENTS_STORED]):
            fields = event.get("fields") or {}
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(fields.items()))
            lines.append(
                f"  [{index}] {event.get('event')} from {event.get('address')}"
                + (f" :: {rendered}" if rendered else "")
            )
    else:
        lines.append("decoded_events: (none)")
    return "\n".join(lines)


def parse_adjudication(raw: typing.Any) -> dict:
    """Defensive parse of the model's answer. Shape is never assumed."""
    if not isinstance(raw, dict):
        raise gl.vm.UserError(
            f"{ERROR_LLM} adjudication was {type(raw).__name__}, expected an object"
        )

    verdict_raw = raw.get("verdict")
    if verdict_raw is None:
        for alternative in ("result", "decision", "answer", "match"):
            if alternative in raw:
                verdict_raw = raw[alternative]
                break
    if isinstance(verdict_raw, bool):
        verdict = VERDICT_MATCH if verdict_raw else VERDICT_MISMATCH
    else:
        verdict = str(verdict_raw or "").strip().lower()
        if verdict in ("matches", "matched", "true", "yes", "confirmed"):
            verdict = VERDICT_MATCH
        elif verdict in ("mismatched", "false", "no", "contradicted"):
            verdict = VERDICT_MISMATCH
        elif verdict in ("unknown", "unclear", "insufficient", "undetermined"):
            verdict = VERDICT_INDETERMINATE
    if verdict not in _VERDICTS:
        raise gl.vm.UserError(f"{ERROR_LLM} unusable verdict {_clip(str(verdict_raw), 40)!r}")

    confidence_raw = raw.get("confidence")
    if confidence_raw is None:
        confidence_raw = raw.get("certainty", 50)
    try:
        confidence = int(round(float(str(confidence_raw).strip().rstrip("%"))))
    except (ValueError, TypeError):
        confidence = 50
    if 0 < confidence <= 1:
        confidence = confidence * 100
    confidence = max(0, min(100, confidence))

    reasoning = raw.get("reasoning")
    if reasoning is None:
        reasoning = raw.get("explanation", "")
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": _clip(str(reasoning), MAX_REASONING_CHARS),
    }


def adjudicate_claim(result: dict, decoded: list, claim: str) -> dict:
    ground_truth = render_ground_truth(result, decoded)
    prompt = (
        "You are verifying whether a natural-language claim about a blockchain "
        "transaction is accurate.\n\n"
        "VERIFIED FACTS. These were fetched from multiple independent block "
        "explorers and already agreed on by consensus. Treat them as ground "
        "truth. Do not contradict them, do not add facts, and do not guess at "
        "anything not listed.\n"
        "-----\n"
        f"{ground_truth}\n"
        "-----\n\n"
        f"CLAIM TO CHECK:\n{claim}\n\n"
        "Decide whether the claim is an accurate description of those facts.\n"
        "Rules:\n"
        "- 'match' means every checkable part of the claim agrees with the facts.\n"
        "- 'mismatch' means some checkable part contradicts the facts, for "
        "example a wrong recipient, wrong amount, wrong asset, wrong direction, "
        "or an event the facts show did not occur.\n"
        "- 'indeterminate' means the facts above are not sufficient to decide, "
        "for example when the claim depends on an event that was not decoded or "
        "on off-chain context.\n"
        "- Judge only the claim's accuracy. Whether the transaction is confirmed "
        "is already decided and is not yours to change.\n"
        "- Amounts are raw integers in the asset's smallest unit. Account for "
        "token decimals when the claim uses a human-readable amount.\n\n"
        'Answer with JSON only: {"verdict": "match"|"mismatch"|"indeterminate", '
        '"confidence": <integer 0-100>, "reasoning": "<one or two sentences>"}'
    )
    raw = gl.nondet.exec_prompt(prompt, response_format="json")
    return parse_adjudication(raw)


# ---------------------------------------------------------------------------
# The pipeline that both the leader and every validator runs
# ---------------------------------------------------------------------------


def run_pipeline(plan: dict, tx_hash: str, claim: str, assertion: str) -> dict:
    """Fetch, cross-check, decode, and optionally adjudicate.

    Called identically by the leader and by each validator. A validator never
    inspects the leader's answer before producing its own, so the two are
    genuinely independent pieces of evidence.
    """
    result = gather(plan, tx_hash)
    result["chain_id"] = plan["chain_id"]
    result["tx_hash"] = tx_hash

    logs = result.pop("logs", [])
    decoder_index = _build_decoder_index()
    decoded = [decode_log(log, decoder_index) for log in logs] if logs else []
    result["decoded_events"] = decoded[:MAX_DECODED_EVENTS_STORED]
    result["decoded_truncated"] = len(decoded) > MAX_DECODED_EVENTS_STORED

    if assertion:
        spec = parse_assertion(assertion)
        # Matching runs over every log, not just the stored summary, so the
        # storage cap never weakens the guarantee.
        satisfied, matches = evaluate_assertion(logs, spec)
        result["assertion"] = canonical_assertion(spec)
        result["assertion_satisfied"] = bool(satisfied and result["confirmed"])
        result["assertion_log_matched"] = len(matches)
        result["assertion_matches"] = matches[:16]
        if not result["confirmed"]:
            result["assertion_note"] = (
                "logs matched but the transaction is not confirmed"
                if satisfied
                else "transaction is not confirmed"
            )
        else:
            result["assertion_note"] = (
                f"{len(matches)} matching log(s)" if satisfied else "no matching log"
            )

    if claim:
        if result["exists"] and result["status"] != STATUS_DISPUTED:
            adjudication = adjudicate_claim(result, decoded, claim)
        else:
            # Nothing to adjudicate against. Decided by code, not by a model.
            adjudication = {
                "verdict": VERDICT_MISMATCH
                if result["status"] == STATUS_ABSENT
                else VERDICT_INDETERMINATE,
                "confidence": 100 if result["status"] == STATUS_ABSENT else 0,
                "reasoning": (
                    "A quorum of sources reports no such transaction on this chain."
                    if result["status"] == STATUS_ABSENT
                    else "Sources disagree about this transaction, so no claim can be judged."
                ),
            }
        result["claim"] = claim
        result["verdict"] = adjudication["verdict"]
        result["confidence"] = adjudication["confidence"]
        result["reasoning"] = adjudication["reasoning"]

    return result


# ---------------------------------------------------------------------------
# Leader / validator comparison
# ---------------------------------------------------------------------------


def _finality_compatible(leader: dict, validator: dict, plan: dict) -> bool:
    """Decide whether two confirmation readings may legitimately differ.

    Confirmation counts only ever grow, so a validator reading ahead of the
    leader is expected. What must not pass is a disagreement too large to be
    explained by the time between the two reads.
    """
    drift = int(plan["drift_tolerance"])
    min_confirmations = int(plan["min_confirmations"])

    if leader["mined"] != validator["mined"]:
        # One side saw the transaction mined and the other did not. Only
        # credible if it had just landed.
        mined_side = leader if leader["mined"] else validator
        if int(mined_side["confirmations"]) > drift:
            return False

    if leader["confirmed"] == validator["confirmed"]:
        return True

    # Exactly one side crossed the threshold. The side that has not must be
    # inside the hysteresis band below it, otherwise the gap is not drift.
    behind = validator if leader["confirmed"] else leader
    return (min_confirmations - int(behind["confirmations"])) <= drift


def results_agree(leader: dict, validator: dict, plan: dict) -> bool:
    """The equivalence principle, in one function.

    Strict about what is signed or security critical, tolerant about what two
    honest reads of the same transaction are expected to disagree on.
    """
    # Existence is never negotiable.
    if bool(leader["exists"]) != bool(validator["exists"]):
        return False

    # A dispute between sources is itself a result both sides must reach.
    if (leader["status"] == STATUS_DISPUTED) != (validator["status"] == STATUS_DISPUTED):
        return False
    if leader["status"] == STATUS_DISPUTED:
        return True

    if not leader["exists"]:
        # Both sides agree a quorum of sources has no such transaction.
        return _claim_fields_agree(leader, validator)

    # Identity is signed by the sender. Two honest views cannot differ.
    if leader["identity_digest"] != validator["identity_digest"]:
        return False
    if bool(leader["confirmed"]) != bool(validator["confirmed"]):
        if not _finality_compatible(leader, validator, plan):
            return False
    elif not _finality_compatible(leader, validator, plan):
        return False

    both_mined = bool(leader["mined"]) and bool(validator["mined"])
    if both_mined:
        # Once both sides see it mined, settlement is immutable. Compare it
        # exactly: this covers status, block, index and every log.
        if leader["settlement_digest"] != validator["settlement_digest"]:
            return False
        if leader["status"] != validator["status"]:
            return False

    # Confirmation counts, gas, labels and timestamps are intentionally not
    # compared. They drift and carry no security meaning here.

    if "assertion_satisfied" in leader or "assertion_satisfied" in validator:
        if leader.get("assertion") != validator.get("assertion"):
            return False
        if bool(leader.get("assertion_satisfied")) != bool(
            validator.get("assertion_satisfied")
        ):
            return False

    return _claim_fields_agree(leader, validator)


def _claim_fields_agree(leader: dict, validator: dict) -> bool:
    if "verdict" not in leader and "verdict" not in validator:
        return True
    if leader.get("verdict") != validator.get("verdict"):
        return False
    gap = abs(int(leader.get("confidence", 0)) - int(validator.get("confidence", 0)))
    return gap <= CONFIDENCE_TOLERANCE


def compare_errors(leader: gl.vm.UserError, validator: gl.vm.UserError) -> bool:
    """Decide whether two failures count as the same failure.

    Deterministic classes must match word for word. A transient class only has
    to be transient on both sides, because the exact wording of a network
    failure is not reproducible. Anything involving the language model never
    agrees, which forces a leader rotation instead of locking in bad state.
    """
    left = leader.message if hasattr(leader, "message") else str(leader)
    right = validator.message if hasattr(validator, "message") else str(validator)

    if left.startswith(ERROR_LLM) or right.startswith(ERROR_LLM):
        return False
    if left.startswith(ERROR_TRANSIENT) and right.startswith(ERROR_TRANSIENT):
        return True
    for prefix in (ERROR_EXPECTED, ERROR_EXTERNAL):
        if left.startswith(prefix) and right.startswith(prefix):
            return left == right
    return False


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@allow_storage
@dataclass
class ChainConfig:
    chain_id: str
    name: str
    family: str
    evm_chain_id: i256
    """Numeric EVM chain id asserted against eth_chainId. -1 for non-EVM."""
    min_confirmations: u256
    drift_tolerance: u256
    """Confirmations a chain may produce during one consensus round."""
    quorum: u256
    enabled: bool
    sources: DynArray[str]
    """One JSON object per source: label, kind, url, api_key."""


@allow_storage
@dataclass
class TxRecord:
    chain_id: str
    tx_hash: str
    exists: bool
    mined: bool
    confirmed: bool
    near_threshold: bool
    status: str
    finality: str
    agreement: str
    from_address: str
    to_address: str
    value_wei: u256
    nonce: i256
    block_number: i256
    tx_index: i256
    method_selector: str
    created_contract: str
    log_count: u256
    confirmations_at_check: u256
    chain_head_at_check: i256
    identity_digest: str
    settlement_digest: str
    sources_queried: u256
    sources_positive: u256
    sources_agreeing: u256
    source_reports: str
    decoded_events: str
    verified_at: u256
    expires_at: u256
    verifier: Address
    epoch: u256


@allow_storage
@dataclass
class ClaimRecord:
    chain_id: str
    tx_hash: str
    claim: str
    verdict: str
    confidence: u256
    reasoning: str
    tx_confirmed: bool
    verified_at: u256
    expires_at: u256
    verifier: Address
    epoch: u256


@allow_storage
@dataclass
class ProofRecord:
    chain_id: str
    tx_hash: str
    assertion: str
    satisfied: bool
    matched_count: u256
    matched_indices: str
    note: str
    tx_confirmed: bool
    verified_at: u256
    expires_at: u256
    verifier: Address
    epoch: u256


# ---------------------------------------------------------------------------
# Events, for indexers and consumer dApps
# ---------------------------------------------------------------------------


class TransactionVerified(gl.Event):
    def __init__(self, chain_id: str, tx_hash: str, /, **blob): ...


class ClaimAdjudicated(gl.Event):
    def __init__(self, chain_id: str, tx_hash: str, /, **blob): ...


class EventProven(gl.Event):
    def __init__(self, chain_id: str, tx_hash: str, /, **blob): ...


class ChainRegistered(gl.Event):
    def __init__(self, chain_id: str, /, **blob): ...


# ---------------------------------------------------------------------------
# Default registry
#
# Every endpoint below was probed directly before being listed: it answers,
# it honours JSON-RPC batching where that is used, and its eth_chainId matches.
# Endpoints that failed that check are deliberately absent. Notably
# cloudflare-eth.com does not honour batch requests, and the Base, Arbitrum and
# Polygon Blockscout instances answer 403.
# ---------------------------------------------------------------------------

DEFAULT_CHAINS: tuple[dict, ...] = (
    {
        "chain_id": "1",
        "name": "Ethereum Mainnet",
        "family": FAMILY_EVM,
        "evm_chain_id": 1,
        # 12 second blocks. 12 confirmations is a little over two minutes.
        "min_confirmations": 12,
        "drift_tolerance": 10,
        "quorum": 2,
        "sources": (
            ("publicnode", KIND_EVM_JSONRPC, "https://ethereum-rpc.publicnode.com"),
            ("drpc", KIND_EVM_JSONRPC, "https://eth.drpc.org"),
            ("blockscout", KIND_BLOCKSCOUT, "https://eth.blockscout.com"),
            ("blastapi", KIND_EVM_JSONRPC, "https://eth-mainnet.public.blastapi.io"),
            ("merkle", KIND_EVM_JSONRPC, "https://eth.merkle.io"),
        ),
    },
    {
        "chain_id": "11155111",
        "name": "Ethereum Sepolia",
        "family": FAMILY_EVM,
        "evm_chain_id": 11155111,
        "min_confirmations": 6,
        "drift_tolerance": 10,
        "quorum": 2,
        "sources": (
            ("publicnode", KIND_EVM_JSONRPC, "https://ethereum-sepolia-rpc.publicnode.com"),
            ("blockscout", KIND_BLOCKSCOUT, "https://eth-sepolia.blockscout.com"),
        ),
    },
    {
        "chain_id": "8453",
        "name": "Base",
        "family": FAMILY_EVM,
        "evm_chain_id": 8453,
        # 2 second blocks.
        "min_confirmations": 60,
        "drift_tolerance": 60,
        "quorum": 2,
        "sources": (
            ("publicnode", KIND_EVM_JSONRPC, "https://base-rpc.publicnode.com"),
            ("base-org", KIND_EVM_JSONRPC, "https://mainnet.base.org"),
            ("drpc", KIND_EVM_JSONRPC, "https://base.drpc.org"),
        ),
    },
    {
        "chain_id": "42161",
        "name": "Arbitrum One",
        "family": FAMILY_EVM,
        "evm_chain_id": 42161,
        # Sub-second blocks, so the confirmation window has to be much wider.
        "min_confirmations": 480,
        "drift_tolerance": 480,
        "quorum": 2,
        "sources": (
            ("arbitrum-foundation", KIND_EVM_JSONRPC, "https://arb1.arbitrum.io/rpc"),
            ("publicnode", KIND_EVM_JSONRPC, "https://arbitrum-one-rpc.publicnode.com"),
            ("drpc", KIND_EVM_JSONRPC, "https://arbitrum.drpc.org"),
        ),
    },
    {
        "chain_id": "10",
        "name": "OP Mainnet",
        "family": FAMILY_EVM,
        "evm_chain_id": 10,
        "min_confirmations": 60,
        "drift_tolerance": 60,
        "quorum": 2,
        "sources": (
            ("publicnode", KIND_EVM_JSONRPC, "https://optimism-rpc.publicnode.com"),
            ("optimism-org", KIND_EVM_JSONRPC, "https://mainnet.optimism.io"),
            ("drpc", KIND_EVM_JSONRPC, "https://optimism.drpc.org"),
            ("blockscout", KIND_BLOCKSCOUT, "https://optimism.blockscout.com"),
        ),
    },
    {
        "chain_id": "137",
        "name": "Polygon PoS",
        "family": FAMILY_EVM,
        "evm_chain_id": 137,
        "min_confirmations": 128,
        "drift_tolerance": 60,
        "quorum": 2,
        "sources": (
            ("publicnode", KIND_EVM_JSONRPC, "https://polygon-bor-rpc.publicnode.com"),
            ("drpc", KIND_EVM_JSONRPC, "https://polygon.drpc.org"),
        ),
    },
    {
        "chain_id": "solana-mainnet",
        "name": "Solana Mainnet Beta",
        "family": FAMILY_SOLANA,
        "evm_chain_id": -1,
        # Adapters request finalized commitment, so anything returned is final.
        # The slot margin only guards against a badly lagging endpoint.
        "min_confirmations": 32,
        "drift_tolerance": 400,
        "quorum": 2,
        "sources": (
            ("solana-foundation", KIND_SOLANA_JSONRPC, "https://api.mainnet-beta.solana.com"),
            ("publicnode", KIND_SOLANA_JSONRPC, "https://solana-rpc.publicnode.com"),
        ),
    },
)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class CrossChainEventVerifier(gl.Contract):
    """Consensus-backed cross-chain transaction and event verification."""

    owner: Address
    paused: bool

    ttl_final: u256
    ttl_confirming: u256
    ttl_absent: u256
    max_sources: u256
    submitters_restricted: bool
    allowed_submitters: TreeMap[Address, bool]

    chain_ids: DynArray[str]
    chains: TreeMap[str, ChainConfig]

    tx_keys: DynArray[str]
    tx_records: TreeMap[str, TxRecord]
    claim_records: TreeMap[str, ClaimRecord]
    proof_records: TreeMap[str, ProofRecord]

    counters: TreeMap[str, u256]

    def __init__(self, register_defaults: bool = True) -> None:
        self.owner = gl.message.sender_address
        self.paused = False
        # A finalised transaction cannot change, so its record is long lived.
        self.ttl_final = u256(30 * 24 * 60 * 60)
        # Anything short of the threshold can still be reorganised out.
        self.ttl_confirming = u256(10 * 60)
        # Absence is the most perishable answer of all: it can become presence.
        self.ttl_absent = u256(3 * 60)
        self.max_sources = u256(5)
        self.submitters_restricted = False

        if register_defaults:
            for spec in DEFAULT_CHAINS:
                self._install_chain(spec)

    # -- internal helpers ---------------------------------------------------

    def _install_chain(self, spec: dict) -> None:
        chain_id = str(spec["chain_id"])
        sources = [
            json.dumps(
                {"label": label, "kind": kind, "url": url, "api_key": ""},
                sort_keys=True,
            )
            for label, kind, url in spec["sources"]
        ]
        config = ChainConfig(
            chain_id=chain_id,
            name=str(spec["name"]),
            family=str(spec["family"]),
            evm_chain_id=i256(int(spec["evm_chain_id"])),
            min_confirmations=u256(int(spec["min_confirmations"])),
            drift_tolerance=u256(int(spec["drift_tolerance"])),
            quorum=u256(int(spec["quorum"])),
            enabled=True,
            sources=sources,
        )
        if chain_id not in self.chains:
            self.chain_ids.append(chain_id)
        self.chains[chain_id] = config

    def _require_owner(self) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} owner only")

    def _require_can_submit(self) -> None:
        if self.paused:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} verifier is paused")
        if self.submitters_restricted:
            sender = gl.message.sender_address
            if sender != self.owner and not self.allowed_submitters.get(sender, False):
                raise gl.vm.UserError(f"{ERROR_EXPECTED} submitter not allowed")

    def _bump(self, name: str) -> None:
        self.counters[name] = u256(self.counters.get(name, u256(0)) + 1)

    def _build_plan(self, chain_id: str) -> dict:
        """Snapshot chain config into plain Python before entering consensus.

        The plan is captured by the leader and validator closures, so it has to
        contain no storage proxies.
        """
        if chain_id not in self.chains:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain {chain_id} is not registered")
        config = self.chains[chain_id]
        if not config.enabled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain {chain_id} is disabled")

        sources: list[dict] = []
        for index in range(len(config.sources)):
            try:
                entry = json.loads(config.sources[index])
            except (ValueError, TypeError):
                continue
            if isinstance(entry, dict) and entry.get("url"):
                sources.append(
                    {
                        "label": str(entry.get("label", f"source{index}")),
                        "kind": str(entry.get("kind", KIND_EVM_JSONRPC)),
                        "url": str(entry["url"]),
                        "api_key": str(entry.get("api_key", "")),
                    }
                )
        if not sources:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain {chain_id} has no sources")

        quorum = int(config.quorum)
        if quorum > len(sources):
            quorum = len(sources)
        if quorum < 1:
            quorum = 1

        return {
            "chain_id": chain_id,
            "family": config.family,
            "expected_evm_chain_id": int(config.evm_chain_id),
            "min_confirmations": int(config.min_confirmations),
            "drift_tolerance": int(config.drift_tolerance),
            "quorum": quorum,
            "max_sources": min(int(self.max_sources), MAX_SOURCES_HARD_CAP, len(sources)),
            "sources": sources,
        }

    def _consensus(self, plan: dict, tx_hash: str, claim: str, assertion: str) -> dict:
        """Run the verification under leader/validator consensus."""

        def leader_fn() -> dict:
            return run_pipeline(plan, tx_hash, claim, assertion)

        def validator_fn(leaders_result: gl.vm.Result) -> bool:
            # Produce our own answer first. The leader's is never an input to
            # it, which is what makes this independent verification rather
            # than a format check.
            mine = run_pipeline(plan, tx_hash, claim, assertion)
            if not isinstance(leaders_result, gl.vm.Return):
                return False
            theirs = leaders_result.calldata
            if not isinstance(theirs, dict):
                return False
            return results_agree(theirs, mine, plan)

        return gl.vm.run_nondet(leader_fn, validator_fn, compare_user_errors=compare_errors)

    def _ttl_for(self, result: dict) -> int:
        if not result["exists"]:
            return int(self.ttl_absent)
        if result["status"] == STATUS_DISPUTED:
            return int(self.ttl_absent)
        if result["confirmed"] and not result["near_threshold"]:
            return int(self.ttl_final)
        return int(self.ttl_confirming)

    def _store_tx(self, result: dict, now: int) -> TxRecord:
        key = _record_key(result["chain_id"], result["tx_hash"])
        previous = self.tx_records.get(key, None)
        epoch = u256(int(previous.epoch) + 1) if previous is not None else u256(1)
        if previous is None:
            self.tx_keys.append(key)

        record = TxRecord(
            chain_id=result["chain_id"],
            tx_hash=result["tx_hash"],
            exists=bool(result["exists"]),
            mined=bool(result["mined"]),
            confirmed=bool(result["confirmed"]),
            near_threshold=bool(result["near_threshold"]),
            status=str(result["status"]),
            finality=str(result["finality"]),
            agreement=str(result["agreement"]),
            from_address=str(result["from"]),
            to_address=str(result["to"]),
            value_wei=u256(int(result["value"])),
            nonce=i256(int(result["nonce"])),
            block_number=i256(int(result["block_number"])),
            tx_index=i256(int(result["tx_index"])),
            method_selector=str(result["selector"]),
            created_contract=str(result["created_contract"]),
            log_count=u256(int(result["log_count"])),
            confirmations_at_check=u256(int(result["confirmations"])),
            chain_head_at_check=i256(int(result["head"])),
            identity_digest=str(result["identity_digest"]),
            settlement_digest=str(result["settlement_digest"]),
            sources_queried=u256(int(result["sources_queried"])),
            sources_positive=u256(int(result["sources_positive"])),
            sources_agreeing=u256(int(result["sources_agreeing"])),
            source_reports=json.dumps(result["source_reports"], sort_keys=True),
            decoded_events=json.dumps(result["decoded_events"], sort_keys=True),
            verified_at=u256(now),
            expires_at=u256(now + self._ttl_for(result)),
            verifier=gl.message.sender_address,
            epoch=epoch,
        )
        self.tx_records[key] = record
        self._bump("verifications")
        if result["confirmed"]:
            self._bump("confirmed")
        if result["status"] == STATUS_DISPUTED:
            self._bump("disputed")
        if not result["exists"]:
            self._bump("absent")
        return record

    def _tx_view(self, record: TxRecord, now: int) -> dict:
        try:
            reports = json.loads(record.source_reports)
        except (ValueError, TypeError):
            reports = []
        try:
            decoded = json.loads(record.decoded_events)
        except (ValueError, TypeError):
            decoded = []
        expired = now >= int(record.expires_at)
        return {
            "found": True,
            "chain_id": record.chain_id,
            "tx_hash": record.tx_hash,
            "exists": bool(record.exists),
            "mined": bool(record.mined),
            "confirmed": bool(record.confirmed),
            "near_threshold": bool(record.near_threshold),
            "valid": bool(record.confirmed) and not expired,
            "expired": expired,
            "status": record.status,
            "finality": record.finality,
            "agreement": record.agreement,
            "from": record.from_address,
            "to": record.to_address,
            "value_wei": str(int(record.value_wei)),
            "nonce": int(record.nonce),
            "block_number": int(record.block_number),
            "tx_index": int(record.tx_index),
            "method_selector": record.method_selector,
            "created_contract": record.created_contract,
            "log_count": int(record.log_count),
            "confirmations_at_check": int(record.confirmations_at_check),
            "chain_head_at_check": int(record.chain_head_at_check),
            "identity_digest": record.identity_digest,
            "settlement_digest": record.settlement_digest,
            "sources_queried": int(record.sources_queried),
            "sources_positive": int(record.sources_positive),
            "sources_agreeing": int(record.sources_agreeing),
            "source_reports": reports,
            "decoded_events": decoded,
            "verified_at": int(record.verified_at),
            "verified_at_iso": _iso_from_epoch(int(record.verified_at)),
            "expires_at": int(record.expires_at),
            "expires_at_iso": _iso_from_epoch(int(record.expires_at)),
            "verifier": record.verifier.as_hex,
            "epoch": int(record.epoch),
        }

    def _absent_tx_view(self, chain_id: str, tx_hash: str) -> dict:
        return {
            "found": False,
            "chain_id": chain_id,
            "tx_hash": tx_hash,
            "exists": False,
            "mined": False,
            "confirmed": False,
            "near_threshold": False,
            "valid": False,
            "expired": False,
            "status": "unverified",
            "finality": FINALITY_UNKNOWN,
            "agreement": AGREEMENT_NONE,
        }

    # -- write: verification ------------------------------------------------

    @gl.public.write
    def verify_transaction(self, chain_id: str, tx_hash: str) -> dict:
        """Verify that a transaction exists and is confirmed on another chain.

        Returns the cached record when a usable one is already on-chain, so
        repeat calls are cheap. Use ``refresh_transaction`` to force a new
        consensus round.
        """
        self._require_can_submit()
        chain = str(chain_id).strip()
        normalised = _norm_tx_hash(tx_hash)
        now = _now_epoch_seconds()

        cached = self.tx_records.get(_record_key(chain, normalised), None)
        if cached is not None and now < int(cached.expires_at):
            self._bump("cache_hits")
            return self._tx_view(cached, now)

        plan = self._build_plan(chain)
        result = self._consensus(plan, normalised, "", "")
        record = self._store_tx(result, now)

        TransactionVerified(
            chain,
            normalised,
            confirmed=bool(record.confirmed),
            status=record.status,
            finality=record.finality,
            block_number=int(record.block_number),
            sources_agreeing=int(record.sources_agreeing),
            epoch=int(record.epoch),
        ).emit()
        return self._tx_view(record, now)

    @gl.public.write
    def refresh_transaction(self, chain_id: str, tx_hash: str) -> dict:
        """Re-run consensus even when a fresh record exists.

        Useful when a transaction was previously seen as pending or disputed
        and is expected to have settled since.
        """
        self._require_can_submit()
        chain = str(chain_id).strip()
        normalised = _norm_tx_hash(tx_hash)
        now = _now_epoch_seconds()

        plan = self._build_plan(chain)
        result = self._consensus(plan, normalised, "", "")
        record = self._store_tx(result, now)

        TransactionVerified(
            chain,
            normalised,
            confirmed=bool(record.confirmed),
            status=record.status,
            finality=record.finality,
            block_number=int(record.block_number),
            sources_agreeing=int(record.sources_agreeing),
            epoch=int(record.epoch),
        ).emit()
        return self._tx_view(record, now)

    @gl.public.write
    def verify_claim(self, chain_id: str, tx_hash: str, claim: str) -> dict:
        """Verify a transaction and adjudicate a natural-language claim about it.

        One consensus round produces both the transaction record and the claim
        verdict. The language model only compares the claim against facts that
        code derived and validators already agreed on. It cannot decide whether
        the transaction exists or is confirmed.
        """
        self._require_can_submit()
        chain = str(chain_id).strip()
        normalised = _norm_tx_hash(tx_hash)
        text = str(claim).strip()
        if not text:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} claim must not be empty")
        if len(text) > MAX_CLAIM_CHARS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} claim is {len(text)} characters, limit is {MAX_CLAIM_CHARS}"
            )
        now = _now_epoch_seconds()

        claim_key = _sub_key(chain, normalised, text)
        cached = self.claim_records.get(claim_key, None)
        if cached is not None and now < int(cached.expires_at):
            self._bump("cache_hits")
            return self._claim_view(cached, now)

        plan = self._build_plan(chain)
        result = self._consensus(plan, normalised, text, "")
        tx_record = self._store_tx(result, now)

        previous = self.claim_records.get(claim_key, None)
        epoch = u256(int(previous.epoch) + 1) if previous is not None else u256(1)
        record = ClaimRecord(
            chain_id=chain,
            tx_hash=normalised,
            claim=text,
            verdict=str(result["verdict"]),
            confidence=u256(int(result["confidence"])),
            reasoning=str(result["reasoning"]),
            tx_confirmed=bool(result["confirmed"]),
            verified_at=u256(now),
            expires_at=u256(now + self._ttl_for(result)),
            verifier=gl.message.sender_address,
            epoch=epoch,
        )
        self.claim_records[claim_key] = record
        self._bump("claims")

        ClaimAdjudicated(
            chain,
            normalised,
            verdict=record.verdict,
            confidence=int(record.confidence),
            tx_confirmed=bool(record.tx_confirmed),
            epoch=int(record.epoch),
        ).emit()

        view = self._claim_view(record, now)
        view["transaction"] = self._tx_view(tx_record, now)
        return view

    @gl.public.write
    def prove_event(self, chain_id: str, tx_hash: str, assertion: str) -> dict:
        """Prove that a transaction emitted a specific log. Fully deterministic.

        No language model is involved. The assertion is matched against every
        log in the receipt, and the result holds only when the transaction is
        also confirmed. This is the strongest of the three modes and the one to
        reach for when the event is known precisely.
        """
        self._require_can_submit()
        chain = str(chain_id).strip()
        normalised = _norm_tx_hash(tx_hash)
        spec = parse_assertion(assertion)
        canonical = canonical_assertion(spec)
        now = _now_epoch_seconds()

        proof_key = _sub_key(chain, normalised, canonical)
        cached = self.proof_records.get(proof_key, None)
        if cached is not None and now < int(cached.expires_at):
            self._bump("cache_hits")
            return self._proof_view(cached, now)

        plan = self._build_plan(chain)
        result = self._consensus(plan, normalised, "", canonical)
        tx_record = self._store_tx(result, now)

        previous = self.proof_records.get(proof_key, None)
        epoch = u256(int(previous.epoch) + 1) if previous is not None else u256(1)
        record = ProofRecord(
            chain_id=chain,
            tx_hash=normalised,
            assertion=canonical,
            satisfied=bool(result["assertion_satisfied"]),
            matched_count=u256(int(result["assertion_log_matched"])),
            matched_indices=json.dumps(result["assertion_matches"]),
            note=str(result["assertion_note"]),
            tx_confirmed=bool(result["confirmed"]),
            verified_at=u256(now),
            expires_at=u256(now + self._ttl_for(result)),
            verifier=gl.message.sender_address,
            epoch=epoch,
        )
        self.proof_records[proof_key] = record
        self._bump("proofs")

        EventProven(
            chain,
            normalised,
            satisfied=bool(record.satisfied),
            matched_count=int(record.matched_count),
            tx_confirmed=bool(record.tx_confirmed),
            epoch=int(record.epoch),
        ).emit()

        view = self._proof_view(record, now)
        view["transaction"] = self._tx_view(tx_record, now)
        return view

    def _claim_view(self, record: ClaimRecord, now: int) -> dict:
        expired = now >= int(record.expires_at)
        return {
            "found": True,
            "chain_id": record.chain_id,
            "tx_hash": record.tx_hash,
            "claim": record.claim,
            "verdict": record.verdict,
            "confidence": int(record.confidence),
            "reasoning": record.reasoning,
            "tx_confirmed": bool(record.tx_confirmed),
            "valid": record.verdict == VERDICT_MATCH
            and bool(record.tx_confirmed)
            and not expired,
            "expired": expired,
            "verified_at": int(record.verified_at),
            "verified_at_iso": _iso_from_epoch(int(record.verified_at)),
            "expires_at": int(record.expires_at),
            "expires_at_iso": _iso_from_epoch(int(record.expires_at)),
            "verifier": record.verifier.as_hex,
            "epoch": int(record.epoch),
        }

    def _proof_view(self, record: ProofRecord, now: int) -> dict:
        try:
            indices = json.loads(record.matched_indices)
        except (ValueError, TypeError):
            indices = []
        expired = now >= int(record.expires_at)
        return {
            "found": True,
            "chain_id": record.chain_id,
            "tx_hash": record.tx_hash,
            "assertion": record.assertion,
            "satisfied": bool(record.satisfied),
            "matched_count": int(record.matched_count),
            "matched_indices": indices,
            "note": record.note,
            "tx_confirmed": bool(record.tx_confirmed),
            "valid": bool(record.satisfied) and not expired,
            "expired": expired,
            "verified_at": int(record.verified_at),
            "verified_at_iso": _iso_from_epoch(int(record.verified_at)),
            "expires_at": int(record.expires_at),
            "expires_at_iso": _iso_from_epoch(int(record.expires_at)),
            "verifier": record.verifier.as_hex,
            "epoch": int(record.epoch),
        }

    # -- read: the consumer-facing surface ---------------------------------

    @gl.public.view
    def is_verified(self, chain_id: str, tx_hash: str) -> bool:
        """The headline question: is this transaction confirmed and still fresh?

        True only when consensus found the transaction, saw it succeed, saw it
        past the chain's confirmation threshold with a quorum of corroborating
        sources, and the record has not expired.
        """
        record = self.tx_records.get(_record_key(str(chain_id).strip(), _norm_tx_hash(tx_hash)), None)
        if record is None:
            return False
        if _now_epoch_seconds() >= int(record.expires_at):
            return False
        return bool(record.confirmed)

    @gl.public.view
    def is_verified_strict(self, chain_id: str, tx_hash: str) -> bool:
        """Like ``is_verified``, but also rejects borderline confirmation depth.

        A record whose confirmation count sat inside the hysteresis band around
        the threshold is flagged ``near_threshold``. For high-value settlement,
        require this instead.
        """
        record = self.tx_records.get(_record_key(str(chain_id).strip(), _norm_tx_hash(tx_hash)), None)
        if record is None:
            return False
        if _now_epoch_seconds() >= int(record.expires_at):
            return False
        return bool(record.confirmed) and not bool(record.near_threshold)

    @gl.public.view
    def get_verification(self, chain_id: str, tx_hash: str) -> dict:
        chain = str(chain_id).strip()
        normalised = _norm_tx_hash(tx_hash)
        record = self.tx_records.get(_record_key(chain, normalised), None)
        if record is None:
            return self._absent_tx_view(chain, normalised)
        return self._tx_view(record, _now_epoch_seconds())

    @gl.public.view
    def record_status(self, chain_id: str, tx_hash: str) -> str:
        """One word describing the cache state: unverified, expired, or the status."""
        record = self.tx_records.get(_record_key(str(chain_id).strip(), _norm_tx_hash(tx_hash)), None)
        if record is None:
            return "unverified"
        if _now_epoch_seconds() >= int(record.expires_at):
            return "expired"
        return record.status

    @gl.public.view
    def is_claim_verified(self, chain_id: str, tx_hash: str, claim: str) -> bool:
        """True when this exact claim was adjudicated a match on a confirmed transaction."""
        key = _sub_key(str(chain_id).strip(), _norm_tx_hash(tx_hash), str(claim).strip())
        record = self.claim_records.get(key, None)
        if record is None:
            return False
        if _now_epoch_seconds() >= int(record.expires_at):
            return False
        return record.verdict == VERDICT_MATCH and bool(record.tx_confirmed)

    @gl.public.view
    def get_claim(self, chain_id: str, tx_hash: str, claim: str) -> dict:
        chain = str(chain_id).strip()
        normalised = _norm_tx_hash(tx_hash)
        key = _sub_key(chain, normalised, str(claim).strip())
        record = self.claim_records.get(key, None)
        if record is None:
            return {
                "found": False,
                "chain_id": chain,
                "tx_hash": normalised,
                "claim": str(claim).strip(),
                "verdict": "unverified",
                "valid": False,
            }
        return self._claim_view(record, _now_epoch_seconds())

    @gl.public.view
    def is_event_proven(self, chain_id: str, tx_hash: str, assertion: str) -> bool:
        """True when this exact event assertion held on a confirmed transaction."""
        canonical = canonical_assertion(parse_assertion(assertion))
        key = _sub_key(str(chain_id).strip(), _norm_tx_hash(tx_hash), canonical)
        record = self.proof_records.get(key, None)
        if record is None:
            return False
        if _now_epoch_seconds() >= int(record.expires_at):
            return False
        return bool(record.satisfied)

    @gl.public.view
    def get_event_proof(self, chain_id: str, tx_hash: str, assertion: str) -> dict:
        chain = str(chain_id).strip()
        normalised = _norm_tx_hash(tx_hash)
        canonical = canonical_assertion(parse_assertion(assertion))
        record = self.proof_records.get(_sub_key(chain, normalised, canonical), None)
        if record is None:
            return {
                "found": False,
                "chain_id": chain,
                "tx_hash": normalised,
                "assertion": canonical,
                "satisfied": False,
                "valid": False,
            }
        return self._proof_view(record, _now_epoch_seconds())

    @gl.public.view
    def compute_topic0(self, signature: str) -> str:
        """keccak256 of an event signature. Pure helper, no consensus needed.

        Lets a builder turn ``Transfer(address,address,uint256)`` into the
        topic0 an assertion needs without hardcoding hashes.
        """
        return topic0_for_signature(signature)

    @gl.public.view
    def normalize_assertion(self, assertion: str) -> str:
        """Canonical form of an assertion, which is also its cache key input.

        Validate an assertion with this before spending a transaction on it.
        """
        return canonical_assertion(parse_assertion(assertion))

    @gl.public.view
    def decode_logs(self, logs_json: str) -> dict:
        """Decode raw logs with the same table the contract uses internally.

        Pure and offline, provided so builders can see exactly how their event
        will be interpreted before submitting a verification.
        """
        try:
            logs = json.loads(str(logs_json))
        except (ValueError, TypeError):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} logs_json is not valid JSON")
        if not isinstance(logs, list):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} logs_json must be a JSON array")
        index = _build_decoder_index()
        return {"decoded": [decode_log(log, index) for log in logs if isinstance(log, dict)]}

    @gl.public.view
    def known_event_signatures(self) -> dict:
        """Every event signature this contract can decode, with its topic0."""
        seen: dict[str, str] = {}
        for signature, _count, _label, _indexed, _data in _DECODER_TABLE:
            seen[signature] = topic0_for_signature(signature)
        return {"signatures": seen, "count": len(seen)}

    # -- read: registry and bookkeeping ------------------------------------

    @gl.public.view
    def get_chain(self, chain_id: str) -> dict:
        chain = str(chain_id).strip()
        if chain not in self.chains:
            return {"found": False, "chain_id": chain}
        config = self.chains[chain]
        sources = []
        for index in range(len(config.sources)):
            try:
                entry = json.loads(config.sources[index])
            except (ValueError, TypeError):
                continue
            sources.append(
                {
                    "label": str(entry.get("label", "")),
                    "kind": str(entry.get("kind", "")),
                    "url": str(entry.get("url", "")),
                    # Never echo a registered API key back out.
                    "has_api_key": bool(entry.get("api_key")),
                }
            )
        return {
            "found": True,
            "chain_id": config.chain_id,
            "name": config.name,
            "family": config.family,
            "evm_chain_id": int(config.evm_chain_id),
            "min_confirmations": int(config.min_confirmations),
            "drift_tolerance": int(config.drift_tolerance),
            "quorum": int(config.quorum),
            "enabled": bool(config.enabled),
            "source_count": len(sources),
            "sources": sources,
        }

    @gl.public.view
    def list_chains(self) -> dict:
        out = []
        for index in range(len(self.chain_ids)):
            chain = self.chain_ids[index]
            config = self.chains[chain]
            out.append(
                {
                    "chain_id": config.chain_id,
                    "name": config.name,
                    "family": config.family,
                    "enabled": bool(config.enabled),
                    "min_confirmations": int(config.min_confirmations),
                    "quorum": int(config.quorum),
                    "source_count": len(config.sources),
                }
            )
        return {"chains": out, "count": len(out)}

    @gl.public.view
    def get_config(self) -> dict:
        return {
            "owner": self.owner.as_hex,
            "paused": bool(self.paused),
            "ttl_final": int(self.ttl_final),
            "ttl_confirming": int(self.ttl_confirming),
            "ttl_absent": int(self.ttl_absent),
            "max_sources": int(self.max_sources),
            "submitters_restricted": bool(self.submitters_restricted),
            "chain_count": len(self.chain_ids),
            "confidence_tolerance": CONFIDENCE_TOLERANCE,
            "max_logs_digested": MAX_LOGS_DIGESTED,
            "max_decoded_events_stored": MAX_DECODED_EVENTS_STORED,
            "source_kinds": list(_SOURCE_KINDS),
        }

    @gl.public.view
    def stats(self) -> dict:
        return {
            "verifications": int(self.counters.get("verifications", u256(0))),
            "confirmed": int(self.counters.get("confirmed", u256(0))),
            "disputed": int(self.counters.get("disputed", u256(0))),
            "absent": int(self.counters.get("absent", u256(0))),
            "claims": int(self.counters.get("claims", u256(0))),
            "proofs": int(self.counters.get("proofs", u256(0))),
            "cache_hits": int(self.counters.get("cache_hits", u256(0))),
            "records": len(self.tx_keys),
        }

    @gl.public.view
    def list_records(self, offset: int, limit: int) -> dict:
        start = max(0, int(offset))
        count = max(0, min(int(limit), 100))
        total = len(self.tx_keys)
        now = _now_epoch_seconds()
        out = []
        for index in range(start, min(start + count, total)):
            record = self.tx_records[self.tx_keys[index]]
            out.append(
                {
                    "chain_id": record.chain_id,
                    "tx_hash": record.tx_hash,
                    "confirmed": bool(record.confirmed),
                    "status": record.status,
                    "finality": record.finality,
                    "expired": now >= int(record.expires_at),
                    "epoch": int(record.epoch),
                }
            )
        return {"records": out, "total": total, "offset": start, "returned": len(out)}

    # -- admin -------------------------------------------------------------

    @gl.public.write
    def register_chain(
        self,
        chain_id: str,
        name: str,
        family: str,
        evm_chain_id: int,
        min_confirmations: int,
        drift_tolerance: int,
        quorum: int,
        sources_json: str,
    ) -> dict:
        """Add or replace a chain, together with its full source list.

        ``sources_json`` is an array of ``{"label","kind","url","api_key"}``.
        ``kind`` must be one of the supported adapters.
        """
        self._require_owner()
        chain = str(chain_id).strip()
        if not chain:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain_id must not be empty")
        fam = str(family).strip().lower()
        if fam not in _FAMILIES:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} family must be one of {', '.join(_FAMILIES)}"
            )
        if int(min_confirmations) < 1:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} min_confirmations must be at least 1")
        if int(drift_tolerance) < 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} drift_tolerance must not be negative")
        if int(quorum) < 1:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} quorum must be at least 1")

        try:
            entries = json.loads(str(sources_json))
        except (ValueError, TypeError):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} sources_json is not valid JSON")
        if not isinstance(entries, list) or not entries:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} sources_json must be a non-empty array")

        prepared = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise gl.vm.UserError(f"{ERROR_EXPECTED} each source must be an object")
            kind = str(entry.get("kind", "")).strip()
            url = str(entry.get("url", "")).strip()
            if kind not in _SOURCE_KINDS:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} unsupported source kind {kind!r}; "
                    f"expected one of {', '.join(_SOURCE_KINDS)}"
                )
            if not url.startswith("https://"):
                raise gl.vm.UserError(f"{ERROR_EXPECTED} source url must be https")
            prepared.append(
                json.dumps(
                    {
                        "label": str(entry.get("label", "source")).strip() or "source",
                        "kind": kind,
                        "url": url,
                        "api_key": str(entry.get("api_key", "")),
                    },
                    sort_keys=True,
                )
            )
        if len(prepared) > MAX_SOURCES_HARD_CAP:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} at most {MAX_SOURCES_HARD_CAP} sources per chain"
            )

        config = ChainConfig(
            chain_id=chain,
            name=str(name).strip() or chain,
            family=fam,
            evm_chain_id=i256(int(evm_chain_id)),
            min_confirmations=u256(int(min_confirmations)),
            drift_tolerance=u256(int(drift_tolerance)),
            quorum=u256(int(quorum)),
            enabled=True,
            sources=prepared,
        )
        if chain not in self.chains:
            self.chain_ids.append(chain)
        self.chains[chain] = config

        ChainRegistered(chain, name=config.name, family=fam, sources=len(prepared)).emit()
        return self.get_chain(chain)

    @gl.public.write
    def add_source(
        self, chain_id: str, label: str, kind: str, url: str, api_key: str
    ) -> dict:
        """Append one source to a registered chain. This is how Etherscan is added."""
        self._require_owner()
        chain = str(chain_id).strip()
        if chain not in self.chains:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain {chain} is not registered")
        adapter_kind = str(kind).strip()
        if adapter_kind not in _SOURCE_KINDS:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} unsupported source kind {adapter_kind!r}; "
                f"expected one of {', '.join(_SOURCE_KINDS)}"
            )
        target = str(url).strip()
        if not target.startswith("https://"):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} source url must be https")
        config = self.chains[chain]
        if len(config.sources) >= MAX_SOURCES_HARD_CAP:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} chain already has the maximum {MAX_SOURCES_HARD_CAP} sources"
            )
        config.sources.append(
            json.dumps(
                {
                    "label": str(label).strip() or "source",
                    "kind": adapter_kind,
                    "url": target,
                    "api_key": str(api_key),
                },
                sort_keys=True,
            )
        )
        return self.get_chain(chain)

    @gl.public.write
    def remove_source(self, chain_id: str, label: str) -> dict:
        """Drop a source by label. Refuses to leave a chain with none."""
        self._require_owner()
        chain = str(chain_id).strip()
        if chain not in self.chains:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain {chain} is not registered")
        config = self.chains[chain]
        target = str(label).strip()

        kept = []
        for index in range(len(config.sources)):
            raw = config.sources[index]
            try:
                entry = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if str(entry.get("label", "")) != target:
                kept.append(raw)
        if len(kept) == len(config.sources):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} no source labelled {target!r}")
        if not kept:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} cannot remove the last source")

        while len(config.sources) > 0:
            config.sources.pop()
        for raw in kept:
            config.sources.append(raw)
        return self.get_chain(chain)

    @gl.public.write
    def set_chain_params(
        self,
        chain_id: str,
        min_confirmations: int,
        drift_tolerance: int,
        quorum: int,
    ) -> dict:
        self._require_owner()
        chain = str(chain_id).strip()
        if chain not in self.chains:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain {chain} is not registered")
        if int(min_confirmations) < 1:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} min_confirmations must be at least 1")
        if int(drift_tolerance) < 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} drift_tolerance must not be negative")
        if int(quorum) < 1:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} quorum must be at least 1")
        config = self.chains[chain]
        config.min_confirmations = u256(int(min_confirmations))
        config.drift_tolerance = u256(int(drift_tolerance))
        config.quorum = u256(int(quorum))
        return self.get_chain(chain)

    @gl.public.write
    def set_chain_enabled(self, chain_id: str, enabled: bool) -> dict:
        self._require_owner()
        chain = str(chain_id).strip()
        if chain not in self.chains:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain {chain} is not registered")
        self.chains[chain].enabled = bool(enabled)
        return self.get_chain(chain)

    @gl.public.write
    def set_cache_policy(self, ttl_final: int, ttl_confirming: int, ttl_absent: int) -> dict:
        """Tune record lifetimes. Shorter means fresher but more consensus rounds."""
        self._require_owner()
        for name, value in (
            ("ttl_final", ttl_final),
            ("ttl_confirming", ttl_confirming),
            ("ttl_absent", ttl_absent),
        ):
            if int(value) < 1:
                raise gl.vm.UserError(f"{ERROR_EXPECTED} {name} must be at least 1 second")
        self.ttl_final = u256(int(ttl_final))
        self.ttl_confirming = u256(int(ttl_confirming))
        self.ttl_absent = u256(int(ttl_absent))
        return self.get_config()

    @gl.public.write
    def set_max_sources(self, max_sources: int) -> dict:
        self._require_owner()
        value = int(max_sources)
        if value < 1 or value > MAX_SOURCES_HARD_CAP:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} max_sources must be 1..{MAX_SOURCES_HARD_CAP}"
            )
        self.max_sources = u256(value)
        return self.get_config()

    @gl.public.write
    def set_paused(self, paused: bool) -> dict:
        """Halt new verifications. Reads keep working, so consumers are unaffected."""
        self._require_owner()
        self.paused = bool(paused)
        return self.get_config()

    @gl.public.write
    def set_submitters_restricted(self, restricted: bool) -> dict:
        """Optionally limit who may trigger a verification. Open by default."""
        self._require_owner()
        self.submitters_restricted = bool(restricted)
        return self.get_config()

    @gl.public.write
    def set_submitter_allowed(self, submitter: Address, allowed: bool) -> dict:
        self._require_owner()
        self.allowed_submitters[submitter] = bool(allowed)
        return {"submitter": submitter.as_hex, "allowed": bool(allowed)}

    @gl.public.write
    def transfer_ownership(self, new_owner: Address) -> dict:
        self._require_owner()
        self.owner = new_owner
        return {"owner": self.owner.as_hex}

    @gl.public.write
    def purge_expired(self, limit: int) -> dict:
        """Delete expired transaction records to reclaim storage.

        Only touches records that are already past their expiry, so it can
        never remove an answer a consumer still relies on.
        """
        self._require_owner()
        budget = max(0, min(int(limit), 200))
        now = _now_epoch_seconds()

        survivors: list[str] = []
        removed = 0
        for index in range(len(self.tx_keys)):
            key = self.tx_keys[index]
            record = self.tx_records.get(key, None)
            if record is None:
                continue
            if removed < budget and now >= int(record.expires_at):
                del self.tx_records[key]
                removed += 1
                continue
            survivors.append(key)

        if removed:
            while len(self.tx_keys) > 0:
                self.tx_keys.pop()
            for key in survivors:
                self.tx_keys.append(key)

        return {"removed": removed, "remaining": len(self.tx_keys)}
