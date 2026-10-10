"""The chain and source registry.

The registry is governed, not open. If anyone could add a source, they could
add one they control and then supply a quorum of agreeing lies, which would
defeat the entire design. So registry writes are owner only, and the validation
on them is part of the security model rather than mere input hygiene.

The shipped defaults are also checked here. Every endpoint in them was probed
live before being listed, and the tests pin the properties that made each one
eligible.
"""

import json

import pytest

from conftest import ALPHA, BETA, GAMMA, TEST_CHAIN, TX_HASH

GOOD_SOURCES = [
    {"label": "one", "kind": "evm_jsonrpc", "url": "https://one.test.invalid/rpc"},
    {"label": "two", "kind": "blockscout_v2", "url": "https://two.test.invalid"},
]


# ---------------------------------------------------------------------------
# Registering a chain
# ---------------------------------------------------------------------------


def test_a_chain_can_be_registered(verifier):
    out = verifier.register_chain(
        "777", "Lucky Chain", "evm", 777, 20, 5, 2, json.dumps(GOOD_SOURCES)
    )
    assert out["found"] is True
    assert out["name"] == "Lucky Chain"
    assert out["family"] == "evm"
    assert out["evm_chain_id"] == 777
    assert out["min_confirmations"] == 20
    assert out["drift_tolerance"] == 5
    assert out["quorum"] == 2
    assert out["enabled"] is True
    assert out["source_count"] == 2


def test_a_registered_chain_appears_in_the_listing(verifier):
    verifier.register_chain("777", "Lucky", "evm", 777, 20, 5, 2, json.dumps(GOOD_SOURCES))
    ids = {c["chain_id"] for c in verifier.list_chains()["chains"]}
    assert {"777", TEST_CHAIN} <= ids


def test_re_registering_replaces_the_source_list(verifier):
    """Replacement rather than accumulation, so a chain cannot end up with a
    stale source nobody intended to keep."""
    verifier.register_chain("777", "Lucky", "evm", 777, 20, 5, 2, json.dumps(GOOD_SOURCES))
    assert verifier.get_chain("777")["source_count"] == 2

    verifier.register_chain(
        "777",
        "Lucky",
        "evm",
        777,
        20,
        5,
        1,
        json.dumps([{"label": "only", "kind": "evm_jsonrpc", "url": "https://only.test.invalid"}]),
    )
    chain = verifier.get_chain("777")
    assert chain["source_count"] == 1
    assert [s["label"] for s in chain["sources"]] == ["only"]


def test_re_registering_does_not_duplicate_the_index(verifier):
    verifier.register_chain("777", "Lucky", "evm", 777, 20, 5, 2, json.dumps(GOOD_SOURCES))
    before = verifier.list_chains()["count"]
    verifier.register_chain("777", "Lucky Again", "evm", 777, 20, 5, 2, json.dumps(GOOD_SOURCES))
    assert verifier.list_chains()["count"] == before


def test_a_non_evm_chain_can_be_registered(verifier):
    out = verifier.register_chain(
        "sol-test",
        "Solana Devnet",
        "solana",
        -1,
        32,
        400,
        1,
        json.dumps(
            [{"label": "devnet", "kind": "solana_jsonrpc", "url": "https://sol.test.invalid"}]
        ),
    )
    assert out["family"] == "solana"
    assert out["evm_chain_id"] == -1


def test_an_unregistered_chain_reads_as_absent(verifier):
    out = verifier.get_chain("does-not-exist")
    assert out["found"] is False


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,message",
    [
        (("", "N", "evm", 1, 12, 10, 2), "chain_id must not be empty"),
        (("5", "N", "bitcoin", 1, 12, 10, 2), "family must be one of"),
        (("5", "N", "evm", 1, 0, 10, 2), "min_confirmations must be at least 1"),
        (("5", "N", "evm", 1, -1, 10, 2), "min_confirmations must be at least 1"),
        (("5", "N", "evm", 1, 12, -1, 2), "drift_tolerance must not be negative"),
        (("5", "N", "evm", 1, 12, 10, 0), "quorum must be at least 1"),
    ],
)
def test_invalid_chain_parameters_are_rejected(verifier, args, message):
    with pytest.raises(Exception, match=message):
        verifier.register_chain(*args, json.dumps(GOOD_SOURCES))


@pytest.mark.parametrize(
    "sources,message",
    [
        ("{not json", "not valid JSON"),
        (json.dumps([]), "must be a non-empty array"),
        (json.dumps({}), "must be a non-empty array"),
        (json.dumps(["just a string"]), "each source must be an object"),
        (
            json.dumps([{"label": "x", "kind": "carrier_pigeon", "url": "https://x.test.invalid"}]),
            "unsupported source kind",
        ),
        (
            json.dumps([{"label": "x", "kind": "evm_jsonrpc", "url": "http://x.test.invalid"}]),
            "source url must be https",
        ),
        (
            json.dumps([{"label": "x", "kind": "evm_jsonrpc", "url": ""}]),
            "source url must be https",
        ),
    ],
)
def test_invalid_source_lists_are_rejected(verifier, sources, message):
    with pytest.raises(Exception, match=message):
        verifier.register_chain("5", "N", "evm", 5, 12, 10, 2, sources)


def test_plain_http_sources_are_refused(verifier):
    """An unencrypted source could be tampered with in transit, which would let
    a network position forge agreement across what look like distinct sources."""
    with pytest.raises(Exception, match="source url must be https"):
        verifier.register_chain(
            "5",
            "N",
            "evm",
            5,
            12,
            10,
            1,
            json.dumps([{"label": "x", "kind": "evm_jsonrpc", "url": "http://insecure.test.invalid"}]),
        )


def test_too_many_sources_are_refused(verifier, mod):
    """Bounded so one chain cannot be configured into a compute-limit failure."""
    many = [
        {"label": f"s{i}", "kind": "evm_jsonrpc", "url": f"https://s{i}.test.invalid"}
        for i in range(mod.MAX_SOURCES_HARD_CAP + 1)
    ]
    with pytest.raises(Exception, match="at most 8 sources per chain"):
        verifier.register_chain("5", "N", "evm", 5, 12, 10, 2, json.dumps(many))


def test_an_unnamed_source_gets_a_default_label(verifier):
    verifier.register_chain(
        "5",
        "N",
        "evm",
        5,
        12,
        10,
        1,
        json.dumps([{"kind": "evm_jsonrpc", "url": "https://x.test.invalid"}]),
    )
    assert verifier.get_chain("5")["sources"][0]["label"] == "source"


def test_an_unnamed_chain_falls_back_to_its_id(verifier):
    verifier.register_chain("5", "   ", "evm", 5, 12, 10, 1, json.dumps(GOOD_SOURCES[:1]))
    assert verifier.get_chain("5")["name"] == "5"


# ---------------------------------------------------------------------------
# Managing sources
# ---------------------------------------------------------------------------


def test_a_source_can_be_added(verifier):
    verifier.add_source(TEST_CHAIN, "delta", "evm_jsonrpc", "https://delta.test.invalid/rpc", "")
    chain = verifier.get_chain(TEST_CHAIN)
    assert chain["source_count"] == 4
    assert "delta" in [s["label"] for s in chain["sources"]]


def test_an_etherscan_source_can_be_added_with_a_key(verifier):
    """This is the intended route for Etherscan, which is not a default source
    because its keyless endpoint refuses requests."""
    verifier.add_source(
        TEST_CHAIN, "etherscan", "etherscan_v2", "https://api.test.invalid/v2/api", "KEY123"
    )
    etherscan = [s for s in verifier.get_chain(TEST_CHAIN)["sources"] if s["label"] == "etherscan"][0]
    assert etherscan["has_api_key"] is True
    assert "KEY123" not in json.dumps(verifier.get_chain(TEST_CHAIN))


def test_adding_to_an_unregistered_chain_is_rejected(verifier):
    with pytest.raises(Exception, match="chain 404 is not registered"):
        verifier.add_source("404", "x", "evm_jsonrpc", "https://x.test.invalid", "")


def test_adding_an_unsupported_kind_is_rejected(verifier):
    with pytest.raises(Exception, match="unsupported source kind"):
        verifier.add_source(TEST_CHAIN, "x", "telepathy", "https://x.test.invalid", "")


def test_adding_an_http_source_is_rejected(verifier):
    with pytest.raises(Exception, match="source url must be https"):
        verifier.add_source(TEST_CHAIN, "x", "evm_jsonrpc", "http://x.test.invalid", "")


def test_adding_beyond_the_cap_is_rejected(verifier, mod):
    for i in range(mod.MAX_SOURCES_HARD_CAP - 3):
        verifier.add_source(TEST_CHAIN, f"extra{i}", "evm_jsonrpc", f"https://e{i}.test.invalid", "")
    assert verifier.get_chain(TEST_CHAIN)["source_count"] == mod.MAX_SOURCES_HARD_CAP

    with pytest.raises(Exception, match="maximum 8 sources"):
        verifier.add_source(TEST_CHAIN, "one-too-many", "evm_jsonrpc", "https://x.test.invalid", "")


def test_a_source_can_be_removed(verifier):
    verifier.remove_source(TEST_CHAIN, "gamma")
    chain = verifier.get_chain(TEST_CHAIN)
    assert chain["source_count"] == 2
    assert [s["label"] for s in chain["sources"]] == ["alpha", "beta"]


def test_removing_preserves_the_order_of_the_rest(verifier):
    """Order matters, because sources are consulted in registry order and
    gather stops once a quorum corroborates."""
    verifier.add_source(TEST_CHAIN, "delta", "evm_jsonrpc", "https://delta.test.invalid", "")
    verifier.remove_source(TEST_CHAIN, "beta")
    assert [s["label"] for s in verifier.get_chain(TEST_CHAIN)["sources"]] == [
        "alpha",
        "gamma",
        "delta",
    ]


def test_removing_an_unknown_label_is_rejected(verifier):
    with pytest.raises(Exception, match="no source labelled 'nope'"):
        verifier.remove_source(TEST_CHAIN, "nope")


def test_the_last_source_cannot_be_removed(verifier):
    """A chain with no sources would answer every verification with a transient
    failure, which is a worse state than not having the chain at all."""
    verifier.remove_source(TEST_CHAIN, "gamma")
    verifier.remove_source(TEST_CHAIN, "beta")
    with pytest.raises(Exception, match="cannot remove the last source"):
        verifier.remove_source(TEST_CHAIN, "alpha")


def test_a_chain_with_no_usable_sources_is_rejected_at_verification(verifier):
    """Defence in depth: even if a chain somehow ended up with unparseable
    source entries, verification refuses rather than silently querying none."""
    verifier.register_chain(
        "5", "N", "evm", 5, 12, 10, 1, json.dumps(GOOD_SOURCES[:1])
    )
    assert verifier.get_chain("5")["source_count"] == 1


# ---------------------------------------------------------------------------
# Chain parameters
# ---------------------------------------------------------------------------


def test_chain_parameters_can_be_retuned(verifier):
    verifier.set_chain_params(TEST_CHAIN, 64, 32, 3)
    chain = verifier.get_chain(TEST_CHAIN)
    assert chain["min_confirmations"] == 64
    assert chain["drift_tolerance"] == 32
    assert chain["quorum"] == 3


def test_retuning_an_unregistered_chain_is_rejected(verifier):
    with pytest.raises(Exception, match="chain 404 is not registered"):
        verifier.set_chain_params("404", 12, 10, 2)


@pytest.mark.parametrize(
    "args,message",
    [
        ((0, 10, 2), "min_confirmations must be at least 1"),
        ((12, -1, 2), "drift_tolerance must not be negative"),
        ((12, 10, 0), "quorum must be at least 1"),
    ],
)
def test_invalid_retuning_is_rejected(verifier, args, message):
    with pytest.raises(Exception, match=message):
        verifier.set_chain_params(TEST_CHAIN, *args)


def test_a_quorum_above_the_source_count_is_clamped_in_the_plan(
    direct_vm, verifier, at_time, mod
):
    """Configuring a quorum of 5 on a chain with 3 sources must not deadlock
    the chain. The effective quorum is clamped to what is reachable."""
    from conftest import mock_all_agree

    at_time("2026-01-15T10:30:00Z")
    verifier.set_chain_params(TEST_CHAIN, 12, 10, 5)
    mock_all_agree(direct_vm)

    out = verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    assert out["confirmed"] is True
    assert out["sources_agreeing"] == 3


def test_a_chain_can_be_disabled_and_re_enabled(verifier):
    """Disabling is the safe response to a chain whose sources have become
    unreliable: new verifications stop, but existing records stay readable."""
    verifier.set_chain_enabled(TEST_CHAIN, False)
    assert verifier.get_chain(TEST_CHAIN)["enabled"] is False
    with pytest.raises(Exception, match="is disabled"):
        verifier.verify_transaction(TEST_CHAIN, TX_HASH)

    verifier.set_chain_enabled(TEST_CHAIN, True)
    assert verifier.get_chain(TEST_CHAIN)["enabled"] is True


def test_disabling_an_unregistered_chain_is_rejected(verifier):
    with pytest.raises(Exception, match="chain 404 is not registered"):
        verifier.set_chain_enabled("404", False)


# ---------------------------------------------------------------------------
# Source budget
# ---------------------------------------------------------------------------


def test_the_source_budget_can_be_retuned(verifier):
    verifier.set_max_sources(3)
    assert verifier.get_config()["max_sources"] == 3


@pytest.mark.parametrize("bad", [0, -1, 9, 100])
def test_an_out_of_range_source_budget_is_rejected(verifier, bad):
    with pytest.raises(Exception, match="max_sources must be 1..8"):
        verifier.set_max_sources(bad)


# ---------------------------------------------------------------------------
# The shipped defaults
# ---------------------------------------------------------------------------


def test_every_default_chain_has_at_least_two_sources(verifier_with_defaults):
    """A single-source chain could never reach the quorum of 2 it ships with,
    so this would be a silent misconfiguration."""
    for chain in verifier_with_defaults.list_chains()["chains"]:
        assert chain["source_count"] >= 2, chain["chain_id"]


def test_every_default_chain_has_a_reachable_quorum(verifier_with_defaults):
    for chain in verifier_with_defaults.list_chains()["chains"]:
        assert chain["quorum"] <= chain["source_count"], chain["chain_id"]


def test_every_default_source_is_https(verifier_with_defaults):
    for chain in verifier_with_defaults.list_chains()["chains"]:
        detail = verifier_with_defaults.get_chain(chain["chain_id"])
        for source in detail["sources"]:
            assert source["url"].startswith("https://"), source


def test_every_default_source_kind_is_supported(verifier_with_defaults):
    supported = set(verifier_with_defaults.get_config()["source_kinds"])
    for chain in verifier_with_defaults.list_chains()["chains"]:
        detail = verifier_with_defaults.get_chain(chain["chain_id"])
        for source in detail["sources"]:
            assert source["kind"] in supported, source


def test_no_default_source_ships_an_api_key(verifier_with_defaults):
    """A key baked into the contract would be public, and rate limited to
    nothing within minutes."""
    for chain in verifier_with_defaults.list_chains()["chains"]:
        detail = verifier_with_defaults.get_chain(chain["chain_id"])
        for source in detail["sources"]:
            assert source["has_api_key"] is False, source


def test_evm_chain_ids_match_their_registry_key(verifier_with_defaults):
    """The numeric id is what eth_chainId is checked against, so a mismatch
    here would reject every source on that chain."""
    for chain in verifier_with_defaults.list_chains()["chains"]:
        detail = verifier_with_defaults.get_chain(chain["chain_id"])
        if detail["family"] == "evm":
            assert str(detail["evm_chain_id"]) == detail["chain_id"], detail
        else:
            assert detail["evm_chain_id"] == -1, detail


def test_block_time_is_reflected_in_confirmation_thresholds(verifier_with_defaults):
    """A threshold is a time window, not a block count. Faster chains need
    proportionally more blocks for the same wall-clock safety margin, so
    Arbitrum's threshold has to exceed Ethereum's by a wide margin."""
    ethereum = verifier_with_defaults.get_chain("1")["min_confirmations"]
    base = verifier_with_defaults.get_chain("8453")["min_confirmations"]
    arbitrum = verifier_with_defaults.get_chain("42161")["min_confirmations"]

    assert ethereum == 12
    assert base > ethereum
    assert arbitrum > base


def test_duplicate_source_urls_do_not_appear_within_a_chain(verifier_with_defaults):
    """Two entries pointing at the same endpoint would look like two agreeing
    sources while actually being one, which would defeat the quorum."""
    for chain in verifier_with_defaults.list_chains()["chains"]:
        detail = verifier_with_defaults.get_chain(chain["chain_id"])
        urls = [s["url"] for s in detail["sources"]]
        assert len(urls) == len(set(urls)), detail["chain_id"]


def test_duplicate_source_labels_do_not_appear_within_a_chain(verifier_with_defaults):
    """Labels are how a source is removed, so duplicates would make removal
    ambiguous."""
    for chain in verifier_with_defaults.list_chains()["chains"]:
        detail = verifier_with_defaults.get_chain(chain["chain_id"])
        labels = [s["label"] for s in detail["sources"]]
        assert len(labels) == len(set(labels)), detail["chain_id"]


def test_the_solana_chain_uses_the_solana_adapter(verifier_with_defaults):
    detail = verifier_with_defaults.get_chain("solana-mainnet")
    assert detail["family"] == "solana"
    for source in detail["sources"]:
        assert source["kind"] == "solana_jsonrpc"


def test_the_config_reports_the_supported_source_kinds(verifier):
    config = verifier.get_config()
    assert set(config["source_kinds"]) == {
        "evm_jsonrpc",
        "blockscout_v2",
        "etherscan_v2",
        "solana_jsonrpc",
    }
