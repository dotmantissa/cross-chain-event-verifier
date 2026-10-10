"""Regressions for evidence quorum, response binding, and reorg invalidation."""
import json
import pytest
from conftest import (
    ALPHA, BETA, GAMMA, TEST_CHAIN, TX_HASH, USDT, BLOCK,
    rpc_transaction, rpc_receipt, evm_batch, mock_rpc, mock_all_agree,
    mock_llm_verdict, erc20_transfer_log,
)
from test_aggregation import found, plan
from test_adapters import evm_source


def test_identity_quorum_cannot_substitute_for_receipt_quorum(mod):
    result = mod.aggregate([found("a", status="pending"), found("b")], plan())
    assert result["sources_agreeing"] == 2
    assert result["confirmed"] is False


def test_deeper_minority_receipt_cannot_override_settlement_quorum(mod):
    result = mod.aggregate([
        found("a", confirmations=64), found("b", confirmations=65),
        found("c", confirmations=999, logs=[erc20_transfer_log(amount=1)]),
    ], plan())
    assert result["confirmed"] is True
    assert result["logs"][0]["data"] == erc20_transfer_log()["data"]
    assert result["sources_settlement_agreeing"] == 2


def test_tied_settlement_quorums_cannot_confirm(mod):
    result = mod.aggregate([
        found("a"), found("b"),
        found("c", logs=[]), found("d", logs=[]),
    ], plan())
    assert result["confirmed"] is False


def test_one_high_head_cannot_cross_threshold(mod):
    result = mod.aggregate([found("a", confirmations=999), found("b", confirmations=1)], plan())
    assert result["confirmed"] is False
    assert result["confirmations"] == 1


@pytest.mark.parametrize("reply", [[], [{"id": 1, "result": "0x1869f"}],
    [{"id": i, "result": {}} for i in (1, 2, 3, 4)],
    [{"id": i, "result": None} for i in (1, 2, 3, 3)]])
def test_malformed_batch_is_never_absence(direct_vm, mod, reply):
    mock_rpc(direct_vm, ALPHA, json.dumps(reply))
    with pytest.raises(Exception, match=r"\[EXTERNAL\]"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


@pytest.mark.parametrize("target,field,value", [
    ("tx", "hash", "0x" + "11" * 32),
    ("receipt", "transactionHash", "0x" + "11" * 32),
    ("receipt", "blockNumber", "0x1"),
    ("receipt", "transactionIndex", "0x1"),
    ("receipt", "blockHash", "0x" + "22" * 32),
])
def test_rpc_response_is_bound_to_requested_transaction(direct_vm, mod, target, field, value):
    tx, receipt = rpc_transaction(), rpc_receipt()
    (tx if target == "tx" else receipt)[field] = value
    mock_rpc(direct_vm, ALPHA, evm_batch(tx, receipt, BLOCK + 64))
    with pytest.raises(Exception, match="mismatch"):
        mod.fetch_evm_jsonrpc(evm_source(), plan(), TX_HASH)


def test_same_endpoint_cannot_count_twice(verifier):
    with pytest.raises(Exception, match="duplicate source url"):
        verifier.add_source(TEST_CHAIN, "again", "evm_jsonrpc", ALPHA + "/", "")
    with pytest.raises(Exception, match="duplicate source url"):
        verifier.register_chain("5", "duplicate", "evm", 5, 12, 10, 2,
            json.dumps([{"url": ALPHA, "kind": "evm_jsonrpc"}] * 2))


def test_source_removal_never_lowers_quorum(verifier):
    verifier.remove_source(TEST_CHAIN, "gamma")
    verifier.remove_source(TEST_CHAIN, "beta")
    with pytest.raises(Exception, match="configured quorum exceeds source budget"):
        verifier.verify_transaction(TEST_CHAIN, TX_HASH)


@pytest.mark.parametrize("mode", ["proof", "claim"])
@pytest.mark.parametrize("change", ["absent", "logs", "confirmations"])
def test_refresh_invalidates_stale_subrecords(direct_vm, verifier, at_time, mode, change):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    payload = json.dumps({"address": USDT}) if mode == "proof" else "a transfer happened"
    if mode == "proof":
        verifier.prove_event(TEST_CHAIN, TX_HASH, payload)
    else:
        mock_llm_verdict(direct_vm)
        verifier.verify_claim(TEST_CHAIN, TX_HASH, payload)
    direct_vm.clear_mocks()
    if change == "absent":
        body = evm_batch(None, None, BLOCK + 64)
    elif change == "logs":
        body = evm_batch(rpc_transaction(), rpc_receipt(logs=[]), BLOCK + 64)
    else:
        body = evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 1)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    verifier.refresh_transaction(TEST_CHAIN, TX_HASH)
    getter = verifier.get_event_proof if mode == "proof" else verifier.get_claim
    predicate = verifier.is_event_proven if mode == "proof" else verifier.is_claim_verified
    assert getter(TEST_CHAIN, TX_HASH, payload)["valid"] is False
    assert predicate(TEST_CHAIN, TX_HASH, payload) is False


@pytest.mark.parametrize("field,value", [
    ("from", "0x" + "99" * 20), ("value", "999"), ("block_number", 1),
    ("chain_id", "1"), ("decoded_events", []), ("near_threshold", True),
])
def test_leader_cannot_forge_stored_fields_while_retaining_digests(direct_vm, verifier, at_time, mod, field, value):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    leader = mod.run_pipeline(verifier._build_plan(TEST_CHAIN), TX_HASH, "", "")
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    leader[field] = value
    assert direct_vm.run_validator(leader_result=leader) is False


def test_blockscout_preserves_rpc_log_order_across_pages(direct_vm, mod):
    import re
    from conftest import blockscout_transaction, blockscout_logs, mock_blockscout
    from test_adapters import blockscout_source
    logs = [erc20_transfer_log(amount=999), erc20_transfer_log(amount=1)]
    page1 = json.loads(blockscout_logs(logs))
    page2 = {"items": [page1["items"].pop()], "next_page_params": None}
    page1["next_page_params"] = {"index": 1}
    detail_url = f"{GAMMA}/api/v2/transactions/{TX_HASH}"
    direct_vm.mock_web(re.escape(detail_url + "/logs?index=1") + "$",
        {"method": "GET", "status": 200, "body": json.dumps(page2)})
    direct_vm.mock_web(re.escape(detail_url + "/logs") + "$",
        {"method": "GET", "status": 200, "body": json.dumps(page1)})
    direct_vm.mock_web(re.escape(detail_url) + "$",
        {"method": "GET", "status": 200, "body": blockscout_transaction()})
    scout = mod.fetch_blockscout_v2(blockscout_source(), plan(), TX_HASH)
    rpc = mod._evm_facts_from_rpc(rpc_transaction(), rpc_receipt(logs=logs))
    assert mod.settlement_digest(scout["facts"]) == mod.settlement_digest(rpc)


def test_gather_continues_until_matching_receipts_reach_quorum(direct_vm, mod):
    from conftest import blockscout_transaction, blockscout_logs, mock_blockscout, TEST_SOURCES
    mock_rpc(direct_vm, ALPHA, evm_batch(rpc_transaction(), rpc_receipt(), BLOCK + 64))
    mock_rpc(direct_vm, BETA, evm_batch(rpc_transaction(), rpc_receipt(logs=[]), BLOCK + 64))
    mock_blockscout(direct_vm, GAMMA, TX_HASH, blockscout_transaction(), blockscout_logs())
    result = mod.gather(dict(plan(), sources=TEST_SOURCES), TX_HASH)
    assert result["confirmed"] is True
    assert result["sources_queried"] == 3


def test_solana_response_must_match_requested_signature(direct_vm, mod):
    from conftest import solana_batch, solana_transaction
    from test_adapters import solana_source, solana_plan
    mock_rpc(direct_vm, ALPHA, solana_batch(solana_transaction(), 455089100))
    with pytest.raises(Exception, match="solana signature mismatch"):
        mod.fetch_solana_jsonrpc(solana_source(), solana_plan(), "different" + "1" * 40)


@pytest.mark.parametrize("confidence", ["nan", "inf", "-inf", "1e309"])
def test_nonfinite_model_confidence_is_a_classified_llm_error(mod, confidence):
    with pytest.raises(Exception, match=r"\[LLM_ERROR\] confidence is not finite"):
        mod.parse_adjudication({"verdict": "match", "confidence": confidence})


@pytest.mark.parametrize("status,exists", [("absent", False), ("disputed", True), ("pending", True), ("failed", True)])
def test_impossible_success_flags_cannot_bypass_early_comparison(mod, status, exists):
    from test_equivalence import result
    honest = result(status=status, exists=exists, confirmed=False, mined=False, confirmations=0)
    forged = dict(honest, confirmed=True)
    assert mod.results_agree(forged, honest, plan()) is False


def test_disputed_claim_verdict_is_compared(mod):
    from test_equivalence import result
    honest = result(status="disputed", confirmed=False, verdict="indeterminate", confidence=0)
    forged = dict(honest, verdict="match", confidence=100)
    assert mod.results_agree(forged, honest, plan()) is False
