"""Smoke checks that the fixtures and the contract boot correctly."""

import json

from conftest import TEST_CHAIN, TRANSFER_TOPIC


def test_deploys_without_defaults(verifier):
    config = verifier.get_config()
    assert config["chain_count"] == 1
    assert config["paused"] is False
    assert config["submitters_restricted"] is False


def test_test_chain_is_registered(verifier):
    chain = verifier.get_chain(TEST_CHAIN)
    assert chain["found"] is True
    assert chain["family"] == "evm"
    assert chain["evm_chain_id"] == 99999
    assert chain["min_confirmations"] == 12
    assert chain["quorum"] == 2
    assert chain["source_count"] == 3
    assert [s["label"] for s in chain["sources"]] == ["alpha", "beta", "gamma"]


def test_default_registry_boots(verifier_with_defaults):
    listing = verifier_with_defaults.list_chains()
    ids = {c["chain_id"] for c in listing["chains"]}
    assert {"1", "8453", "42161", "10", "137", "11155111", "solana-mainnet"} <= ids
    assert listing["count"] >= 7
    for chain in listing["chains"]:
        assert chain["source_count"] >= 2, chain
        assert chain["enabled"] is True


def test_module_helpers_are_reachable(mod):
    assert mod.topic0_for_signature("Transfer(address,address,uint256)") == TRANSFER_TOPIC
    assert callable(mod.aggregate)
    assert callable(mod.results_agree)
    assert callable(mod.compare_errors)


def test_known_signatures_exposed(verifier):
    known = verifier.known_event_signatures()
    assert known["count"] >= 15
    assert known["signatures"]["Transfer(address,address,uint256)"] == TRANSFER_TOPIC
