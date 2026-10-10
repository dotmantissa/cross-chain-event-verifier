"""Source adapters.

Each adapter turns one explorer's wire format into the same normalised shape.
Normalising before comparing is what lets two explorers that disagree about
formatting still agree about facts.

Payloads here are shaped from responses captured from the live endpoints, so a
test passing here exercises the same parsing path production data takes.

The adapters are also where every failure mode is classified. A source that is
rate limited, misconfigured, or serving the wrong chain has to produce the
right error class, because that class is what decides whether two validators
hitting different failures can still agree.
"""

import json

import pytest

from conftest import (
    ALPHA,
    GAMMA,
    RECIPIENT,
    SENDER,
    TEST_CHAIN_HEX,
    TX_HASH,
    USDT,
    blockscout_logs,
    blockscout_transaction,
    erc20_transfer_log,
    evm_batch,
    mock_blockscout,
    mock_rpc,
    rpc_receipt,
    rpc_transaction,
    solana_batch,
    solana_transaction,
)

BLOCK = 16777216


def plan(**overrides) -> dict:
    base = {
        "chain_id": "99999",
        "quorum": 2,
        "min_confirmations": 12,
        "drift_tolerance": 10,
        "max_sources": 5,
        "sources": [],
        "expected_evm_chain_id": 99999,
        "family": "evm",
    }
    base.update(overrides)
    return base


def evm_source(url: str = ALPHA) -> dict:
    return {"label": "alpha", "kind": "evm_jsonrpc", "url": url, "api_key": ""}


def blockscout_source(url: str = GAMMA) -> dict:
    return {"label": "gamma", "kind": "blockscout_v2", "url": url, "api_key": ""}


# ---------------------------------------------------------------------------
# EVM JSON-RPC
# ---------------------------------------------------------------------------


def test_evm_adapter_normalises_a_confirmed_transaction(direct_vm, mod):
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64))
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)

    assert out["outcome"] == "found"
    facts = out["facts"]
    assert facts["status"] == "success"
    assert facts["from"] == SENDER
    assert facts["to"] == USDT
    assert facts["value"] == "0"
    assert facts["nonce"] == 79
    assert facts["block_number"] == BLOCK
    assert facts["selector"] == "0xa9059cbb"
    assert len(facts["logs"]) == 1
    assert out["confirmations"] == 65


def test_evm_adapter_computes_confirmations_from_the_head(direct_vm, mod):
    """Standard RPC does not report a confirmation count, so it is derived.
    Inclusive of the containing block, matching how explorers count."""
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(), BLOCK))
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["confirmations"] == 1


def test_evm_adapter_reports_absence(direct_vm, mod):
    mock_rpc(direct_vm, ALPHA, evm_batch(None, None, BLOCK + 64))
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["outcome"] == "absent"
    assert "facts" not in out


def test_evm_adapter_reads_a_reverted_transaction(direct_vm, mod):
    mock_rpc(
        direct_vm,
        ALPHA,
        evm_batch(rpc_transaction(), rpc_receipt(status=0), BLOCK + 64),
    )
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["facts"]["status"] == "failed"


def test_evm_adapter_reads_a_pending_transaction(direct_vm, mod):
    """A transaction in the mempool has a null blockNumber and no receipt."""
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(block=None), None, BLOCK + 64))
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["outcome"] == "found"
    assert out["facts"]["status"] == "pending"
    assert out["facts"]["block_number"] == -1
    assert out["confirmations"] == 0


def test_evm_adapter_handles_a_mined_transaction_with_no_receipt(direct_vm, mod):
    """The observed pruned-backend case: the transaction object says it is
    mined, but this backend has no receipt for it. Inclusion is reported
    without settlement, rather than either being invented."""
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), None, BLOCK + 64))
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["outcome"] == "found"
    assert out["facts"]["block_number"] == BLOCK
    assert out["facts"]["status"] == "pending"
    assert out["facts"]["logs"] == []
    assert "no receipt" in out["note"]


def test_evm_adapter_reads_a_contract_creation(direct_vm, mod):
    created = "0x" + "ab" * 20
    mock_rpc(
        direct_vm,
        ALPHA,
        evm_batch(
            rpc_transaction(to=None),
            rpc_receipt(to=None, contract_address=created),
            BLOCK + 64,
        ),
    )
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["facts"]["to"] == ""
    assert out["facts"]["created_contract"] == created


def test_evm_adapter_accepts_a_pre_byzantium_receipt(direct_vm, mod):
    """Receipts before Byzantium carry no status field. The presence of a
    receipt is treated as inclusion rather than as a failure."""
    receipt = rpc_receipt()
    del receipt["status"]
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), receipt, BLOCK + 64))
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["facts"]["status"] == "success"


# -- chain binding ----------------------------------------------------------


def test_evm_adapter_rejects_a_source_serving_another_chain(direct_vm, mod):
    """eth_chainId travels in the same batch and is checked against the
    registry, so a swapped or misconfigured endpoint cannot quietly serve a
    transaction from a different chain. This is classified EXTERNAL because it
    is deterministic: every validator hitting that endpoint sees it."""
    mock_rpc(
        direct_vm,
        ALPHA,
        evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64, chain_id_hex="0x1"),
    )
    with pytest.raises(Exception, match=r"\[EXTERNAL\] source serves chain 1, expected 99999"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


def test_evm_adapter_skips_the_chain_check_when_not_applicable(direct_vm, mod):
    """A chain registered without a numeric id cannot be checked this way."""
    mock_rpc(
        direct_vm,
        ALPHA,
        evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64, chain_id_hex="0x1"),
    )
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(expected_evm_chain_id=-1), TX_HASH)
    assert out["outcome"] == "found"


def test_evm_adapter_tolerates_a_source_that_omits_chain_id(direct_vm, mod):
    """Some endpoints answer the batch but leave eth_chainId null. That is not
    grounds for rejecting an otherwise valid reading."""
    body = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "result": None},
            {"jsonrpc": "2.0", "id": 2, "result": rpc_transaction()},
            {"jsonrpc": "2.0", "id": 3, "result": rpc_receipt()},
            {"jsonrpc": "2.0", "id": 4, "result": hex(BLOCK + 64)},
        ]
    )
    mock_rpc(direct_vm, ALPHA, body)
    out = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert out["outcome"] == "found"


# -- failure classification -------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, r"\[EXTERNAL\] source rejected request \(400\)"),
        (401, r"\[EXTERNAL\] source rejected request \(401\)"),
        (403, r"\[EXTERNAL\] source rejected request \(403\)"),
        (429, r"\[TRANSIENT\] source rate limited \(429\)"),
        (500, r"\[TRANSIENT\] source unavailable \(500\)"),
        (502, r"\[TRANSIENT\] source unavailable \(502\)"),
        (503, r"\[TRANSIENT\] source unavailable \(503\)"),
    ],
)
def test_http_status_codes_map_to_the_right_error_class(direct_vm, mod, status, expected):
    """The classification decides whether two validators hitting different
    failures can agree. A 4xx is deterministic and must match exactly; a 5xx or
    a rate limit is not, and agreeing that it happened is enough."""
    mock_rpc(direct_vm, ALPHA, "{}", status=status)
    with pytest.raises(Exception, match=expected):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


def test_rate_limiting_is_transient_not_external(direct_vm, mod):
    """429 sits in the 4xx range but is explicitly transient: retrying later
    genuinely can succeed, so validators should not be forced to match wording."""
    mock_rpc(direct_vm, ALPHA, "{}", status=429)
    with pytest.raises(Exception) as excinfo:
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    assert "[TRANSIENT]" in str(excinfo.value)


def test_malformed_json_is_transient(direct_vm, mod):
    """A truncated or proxy-mangled body is a transport problem, so it is
    retryable rather than a statement about the transaction."""
    mock_rpc(direct_vm, ALPHA, "not json at all")
    with pytest.raises(Exception, match=r"\[TRANSIENT\] source returned malformed JSON"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


def test_empty_body_is_transient(direct_vm, mod):
    mock_rpc(direct_vm, ALPHA, "")
    with pytest.raises(Exception, match=r"\[TRANSIENT\] source returned an empty body"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


def test_a_source_without_batch_support_is_rejected(direct_vm, mod):
    """Observed on cloudflare-eth.com, which answers a batch with a single
    object. Detecting it explicitly beats silently misparsing the reply, and it
    is why that endpoint is not in the default registry."""
    mock_rpc(direct_vm, ALPHA, json.dumps({"jsonrpc": "2.0", "id": 1, "result": None}))
    with pytest.raises(Exception, match=r"\[EXTERNAL\] source does not support JSON-RPC batch"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


def test_a_json_rpc_error_object_is_external(direct_vm, mod):
    body = json.dumps(
        [{"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "Unauthorized"}}]
    )
    mock_rpc(direct_vm, ALPHA, body)
    with pytest.raises(Exception, match=r"\[EXTERNAL\] rpc error: Unauthorized"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


def test_an_absurd_log_count_is_rejected(direct_vm, mod):
    """A cap breach is a property of the transaction, not of one source, so it
    is EXPECTED and stops the whole verification rather than being recorded as
    one source having a bad day."""
    too_many = [erc20_transfer_log(amount=i) for i in range(mod.MAX_LOGS_DIGESTED + 1)]
    mock_rpc(
        direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(logs=too_many), BLOCK + 64)
    )
    with pytest.raises(Exception, match=r"\[EXPECTED\] transaction has \d+ logs"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


# ---------------------------------------------------------------------------
# Blockscout v2
# ---------------------------------------------------------------------------


def test_blockscout_adapter_normalises_a_confirmed_transaction(direct_vm, mod):
    mock_blockscout(
        direct_vm, GAMMA, TX_HASH, blockscout_transaction(confirmations=64), blockscout_logs()
    )
    out = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)

    assert out["outcome"] == "found"
    facts = out["facts"]
    assert facts["status"] == "success"
    assert facts["from"] == SENDER
    assert facts["to"] == USDT
    assert facts["block_number"] == BLOCK
    assert out["confirmations"] == 64


def test_blockscout_output_matches_rpc_output_exactly(direct_vm, mod):
    """The property the whole design depends on. Blockscout reports checksum
    addresses and decimal values; RPC reports lowercase and hex. After
    normalisation both must produce the same identity digest, or two honest
    sources would look like a dispute."""
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64))
    mock_blockscout(
        direct_vm, GAMMA, TX_HASH, blockscout_transaction(confirmations=65), blockscout_logs()
    )

    from_rpc = mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)
    from_scout = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)

    assert mod.identity_digest(from_rpc["facts"]) == mod.identity_digest(from_scout["facts"])
    assert mod.settlement_digest(from_rpc["facts"]) == mod.settlement_digest(
        from_scout["facts"]
    )


def test_blockscout_adapter_strips_padded_null_topics(direct_vm, mod):
    """Blockscout pads the topics array to four entries with nulls. Leaving
    them in would change the topic count and so break ERC-20 decoding."""
    mock_blockscout(
        direct_vm, GAMMA, TX_HASH, blockscout_transaction(), blockscout_logs()
    )
    out = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)
    assert len(out["facts"]["logs"][0]["topics"]) == 3


def test_blockscout_adapter_reports_absence(direct_vm, mod):
    """Blockscout answers 200 with a message body rather than a 404."""
    mock_blockscout(direct_vm, GAMMA, TX_HASH, json.dumps({"message": "Not found"}))
    out = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)
    assert out["outcome"] == "absent"


def test_blockscout_adapter_reads_a_reverted_transaction(direct_vm, mod):
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(result="Reverted", status="error"),
        blockscout_logs(),
    )
    out = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)
    assert out["facts"]["status"] == "failed"


def test_blockscout_adapter_reads_a_pending_transaction(direct_vm, mod):
    mock_blockscout(
        direct_vm,
        GAMMA,
        TX_HASH,
        blockscout_transaction(block=None, confirmations=0, result="pending", status=""),
    )
    out = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)
    assert out["facts"]["status"] == "pending"
    assert out["facts"]["block_number"] == -1
    # A pending transaction has no logs to fetch, so no second request is made.
    assert out["facts"]["logs"] == []


def test_blockscout_adapter_derives_a_head_from_confirmations(direct_vm, mod):
    """Blockscout reports confirmations directly but not the chain head, so the
    head is reconstructed for reporting."""
    mock_blockscout(
        direct_vm, GAMMA, TX_HASH, blockscout_transaction(confirmations=100), blockscout_logs()
    )
    out = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)
    assert out["head"] == BLOCK + 100


def test_blockscout_failures_are_classified(direct_vm, mod):
    import re

    direct_vm.mock_web(
        re.escape(f"{GAMMA}/api/v2/transactions/{TX_HASH}"),
        {"method": "GET", "status": 403, "body": "forbidden"},
    )
    with pytest.raises(Exception, match=r"\[EXTERNAL\] source rejected request \(403\)"):
        mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)


# ---------------------------------------------------------------------------
# Solana JSON-RPC
# ---------------------------------------------------------------------------


def solana_source(url: str = ALPHA) -> dict:
    return {"label": "sol", "kind": "solana_jsonrpc", "url": url, "api_key": ""}


def solana_plan() -> dict:
    return plan(family="solana", expected_evm_chain_id=-1, chain_id="solana-mainnet")


def test_solana_adapter_normalises_a_finalized_transaction(direct_vm, mod):
    mock_rpc(direct_vm, ALPHA, solana_batch(solana_transaction(), 455089100))
    out = mod.fetch_solana_jsonrpc(solana_source(), solana_plan(), "sig" + "1" * 40)

    assert out["outcome"] == "found"
    facts = out["facts"]
    assert facts["status"] == "success"
    assert facts["block_number"] == 455089019
    assert facts["from"] == "SA1LFXr4os2P4VKGUyRv84uFfuUYgcQkFh2uA4SmRcr"
    assert len(facts["accounts"]) == 3
    assert facts["programs"] == ["Vote111111111111111111111111111111111111111"]
    assert out["confirmations"] == 82


def test_solana_log_messages_become_logs_without_topics(direct_vm, mod):
    """Solana has no EVM-style topics, so each program log line is carried in
    the data field. Structured assertions reach it through data_contains."""
    mock_rpc(direct_vm, ALPHA, solana_batch(solana_transaction(), 455089100))
    out = mod.fetch_solana_jsonrpc(solana_source(), solana_plan(), "sig" + "1" * 40)
    logs = out["facts"]["logs"]
    assert len(logs) == 2
    assert logs[0]["topics"] == []
    assert "invoke" in logs[0]["data"]


def test_solana_adapter_reads_a_failed_transaction(direct_vm, mod):
    mock_rpc(
        direct_vm,
        ALPHA,
        solana_batch(solana_transaction(err={"InstructionError": [0, "Custom"]}), 455089100),
    )
    out = mod.fetch_solana_jsonrpc(solana_source(), solana_plan(), "sig" + "1" * 40)
    assert out["facts"]["status"] == "failed"


def test_solana_adapter_reports_absence(direct_vm, mod):
    """The request asks for finalized commitment, so a null result means the
    cluster has not finalised it rather than that it never existed."""
    mock_rpc(direct_vm, ALPHA, solana_batch(None, 455089100))
    out = mod.fetch_solana_jsonrpc(solana_source(), solana_plan(), "sig" + "1" * 40)
    assert out["outcome"] == "absent"
    assert "finalized" in out["note"]


def test_solana_errors_are_classified(direct_vm, mod):
    body = json.dumps(
        [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid param"}}]
    )
    mock_rpc(direct_vm, ALPHA, body)
    with pytest.raises(Exception, match=r"\[EXTERNAL\] solana rpc error: Invalid param"):
        mod.fetch_solana_jsonrpc(solana_source(), solana_plan(), "sig" + "1" * 40)


# ---------------------------------------------------------------------------
# Etherscan v2
# ---------------------------------------------------------------------------


def etherscan_source(key: str = "SECRETKEY") -> dict:
    return {
        "label": "etherscan",
        "kind": "etherscan_v2",
        "url": "https://api.test.invalid/v2/api",
        "api_key": key,
    }


def test_etherscan_adapter_requires_a_key(direct_vm, mod):
    """The keyless endpoint answers 'Missing/Invalid API Key', which is why
    Etherscan is not among the default sources. Failing before the request is
    made keeps the error deterministic."""
    with pytest.raises(Exception, match=r"\[EXTERNAL\] etherscan source has no api key"):
        mod.fetch_etherscan_v2(etherscan_source(key=""), plan(), TX_HASH)


def test_etherscan_adapter_requires_a_numeric_chain_id(direct_vm, mod):
    with pytest.raises(Exception, match=r"\[EXPECTED\] etherscan source needs a numeric chain id"):
        mod.fetch_etherscan_v2(etherscan_source(), plan(expected_evm_chain_id=-1), TX_HASH)


def test_etherscan_adapter_normalises_a_transaction(direct_vm, mod):
    import re

    direct_vm.mock_web(
        r"action=eth_getTransactionByHash",
        {"method": "GET", "status": 200, "body": json.dumps({"result": rpc_transaction()})},
    )
    direct_vm.mock_web(
        r"action=eth_getTransactionReceipt",
        {"method": "GET", "status": 200, "body": json.dumps({"result": rpc_receipt()})},
    )
    direct_vm.mock_web(
        r"action=eth_blockNumber",
        {"method": "GET", "status": 200, "body": json.dumps({"result": hex(BLOCK + 64)})},
    )
    out = mod.fetch_etherscan_v2(etherscan_source(), plan(), TX_HASH)
    assert out["outcome"] == "found"
    assert out["facts"]["from"] == SENDER
    assert out["confirmations"] == 65


def test_etherscan_notok_is_external(direct_vm, mod):
    direct_vm.mock_web(
        r"action=eth_getTransactionByHash",
        {
            "method": "GET",
            "status": 200,
            "body": json.dumps(
                {"status": "0", "message": "NOTOK", "result": "Missing/Invalid API Key"}
            ),
        },
    )
    with pytest.raises(Exception, match=r"\[EXTERNAL\] etherscan refused: Missing/Invalid API Key"):
        mod.fetch_etherscan_v2(etherscan_source(), plan(), TX_HASH)


def test_the_api_key_is_never_echoed_back(verifier, mod):
    """A registered key must not be readable from a view, since view calls are
    unauthenticated."""
    verifier.add_source("99999", "etherscan", "etherscan_v2", "https://api.test.invalid/v2/api", "SUPERSECRET")
    chain = verifier.get_chain("99999")
    serialised = json.dumps(chain)
    assert "SUPERSECRET" not in serialised
    etherscan = [s for s in chain["sources"] if s["label"] == "etherscan"][0]
    assert etherscan["has_api_key"] is True


# ---------------------------------------------------------------------------
# observe_source: isolating one source's failure
# ---------------------------------------------------------------------------


def test_a_failing_source_is_recorded_not_raised(direct_vm, mod):
    """One source failing must not abort the verification, or a single
    unavailable explorer could deny service to the whole primitive."""
    mock_rpc(direct_vm, ALPHA, "{}", status=503)
    out = mod.observe_source(evm_source(), plan(), TX_HASH)
    assert out["outcome"] == "error"
    assert out["label"] == "alpha"
    assert "[TRANSIENT]" in out["note"]


def test_an_external_failure_is_also_isolated(direct_vm, mod):
    mock_rpc(direct_vm, ALPHA, "{}", status=403)
    out = mod.observe_source(evm_source(), plan(), TX_HASH)
    assert out["outcome"] == "error"
    assert "[EXTERNAL]" in out["note"]


def test_a_transaction_level_rejection_is_not_isolated(direct_vm, mod):
    """An EXPECTED error is a statement about the transaction itself, so it has
    to propagate rather than be swallowed as one source's problem."""
    too_many = [erc20_transfer_log(amount=i) for i in range(mod.MAX_LOGS_DIGESTED + 1)]
    mock_rpc(
        direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(logs=too_many), BLOCK + 64)
    )
    with pytest.raises(Exception, match=r"\[EXPECTED\]"):
        mod.observe_source(evm_source(), plan(), TX_HASH)


def test_an_unsupported_source_kind_is_reported(direct_vm, mod):
    out = mod.observe_source(
        {"label": "weird", "kind": "carrier_pigeon", "url": "https://x.test.invalid"},
        plan(),
        TX_HASH,
    )
    assert out["outcome"] == "error"
    assert "unsupported source kind" in out["note"]


def test_the_source_label_survives_every_path(direct_vm, mod):
    """Labels are the audit trail, so they have to be attached on success and
    on failure alike."""
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64))
    assert mod.observe_source(evm_source(), plan(), TX_HASH)["label"] == "alpha"

    direct_vm.clear_mocks()
    mock_rpc(direct_vm, ALPHA, "{}", status=503)
    assert mod.observe_source(evm_source(), plan(), TX_HASH)["label"] == "alpha"


# ---------------------------------------------------------------------------
# gather: early exit
# ---------------------------------------------------------------------------


def test_gather_stops_once_a_quorum_corroborates(direct_vm, mod):
    """Querying every source when two already agree wastes time and rate
    limit. Stopping early is safe because the comparison that follows is over
    the derived verdict, not over which sources answered."""
    body = evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, "https://beta-rpc.test.invalid/rpc", body)

    sources = [
        evm_source(ALPHA),
        evm_source("https://beta-rpc.test.invalid/rpc"),
        # Never queried, and deliberately has no mock. If gather reached it the
        # test would fail with a missing-mock error.
        evm_source("https://never-queried.test.invalid/rpc"),
    ]
    out = mod.gather(plan(sources=sources, quorum=2), TX_HASH)
    assert out["sources_queried"] == 2
    assert out["confirmed"] is True


def test_gather_continues_past_a_failing_source(direct_vm, mod):
    body = evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, "{}", status=503)
    mock_rpc(direct_vm, "https://beta-rpc.test.invalid/rpc", body)
    mock_rpc(direct_vm, "https://third.test.invalid/rpc", body)

    sources = [
        evm_source(ALPHA),
        evm_source("https://beta-rpc.test.invalid/rpc"),
        evm_source("https://third.test.invalid/rpc"),
    ]
    out = mod.gather(plan(sources=sources, quorum=2), TX_HASH)
    assert out["sources_queried"] == 3
    assert out["sources_errored"] == 1
    assert out["confirmed"] is True


def test_gather_respects_the_source_budget(direct_vm, mod):
    """max_sources caps how many sources a single verification may consult."""
    body = evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64)
    mock_rpc(direct_vm, ALPHA, body)

    sources = [evm_source(ALPHA), evm_source("https://unmocked.test.invalid/rpc")]
    out = mod.gather(plan(sources=sources, quorum=1, max_sources=1), TX_HASH)
    assert out["sources_queried"] == 1
