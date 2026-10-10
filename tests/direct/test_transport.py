"""Transport regressions independent of the contract's direct VM."""
from types import SimpleNamespace
import pytest
from genlayer_py.abi import calldata
from genlayer_py.contracts.utils import make_calldata_object
from genlayer_py.exceptions import GenLayerError
from genlayer_py.types import TransactionHashVariant
from scripts.transport import install_studionet_read_compat


class Provider:
    url = "https://studio.genlayer.com/api"

    def make_request(self, method, params):
        assert method == "gen_call"
        self.request = params[0]
        # This simulates the RPC boundary only, not contract validation.
        obj = calldata.decode(bytes.fromhex(self.request["data"][2:]))
        assert obj["method"] == "normalize_assertion"
        assert obj["args"] == ["a" * 400]
        return {"result": calldata.encode({"ok": True}).hex()}


def test_long_reads_use_raw_calldata_and_preserve_state_and_sender():
    provider = Provider()
    client = install_studionet_read_compat(SimpleNamespace(provider=provider, local_account=None))
    account = SimpleNamespace(address="0x" + "11" * 20)
    result = client.read_contract(address="0x" + "22" * 20, function_name="normalize_assertion",
        args=["a" * 400], account=account,
        transaction_hash_variant=TransactionHashVariant.LATEST_FINAL)
    assert result == {"ok": True}
    assert provider.request["from"] == account.address
    assert provider.request["transaction_hash_variant"] == "latest-final"


def test_other_endpoints_keep_the_sdk_transport():
    original = object()
    client = SimpleNamespace(provider=SimpleNamespace(url="http://127.0.0.1:4000/api"), read_contract=original)
    install_studionet_read_compat(client)
    assert client.read_contract is original


def test_sender_is_required_for_reads():
    client = install_studionet_read_compat(SimpleNamespace(provider=Provider(), local_account=None))
    with pytest.raises(GenLayerError, match="No account"):
        client.read_contract(address="0x" + "22" * 20, function_name="normalize_assertion")



def test_typed_json_arguments_keep_chain_ids_and_hashes_as_strings():
    from scripts.ccev import convert_arguments
    value = ["1", "0x" + "ab" * 32, {"$address": "0x" + "11" * 20}]
    result = convert_arguments(value)
    assert result[:2] == value[:2]
    assert result[2].as_bytes == bytes.fromhex("11" * 20)


def test_typed_json_arguments_preserve_unrelated_dicts():
    from scripts.ccev import convert_arguments
    assert convert_arguments({"address": "0x" + "11" * 20}) == {"address": "0x" + "11" * 20}


@pytest.mark.parametrize('method,retries', [('eth_getTransactionByHash', 3), ('eth_sendRawTransaction', 1)])
def test_transport_failures_retry_only_reads(monkeypatch, method, retries):
    import requests
    from scripts import transport
    monkeypatch.setattr(transport.time, 'sleep', lambda _: None)
    attempts = []

    def request(**kwargs):
        attempts.append(kwargs)
        try:
            raise requests.exceptions.ConnectionError('temporary DNS failure')
        except requests.exceptions.ConnectionError as cause:
            raise GenLayerError('transport failed') from cause

    client = transport.install_rpc_read_retries(SimpleNamespace(
        provider=SimpleNamespace(make_request=request)))
    with pytest.raises(GenLayerError):
        client.provider.make_request(method=method, params=['unchanged'])
    assert len(attempts) == retries
    assert all(call == {'method': method, 'params': ['unchanged']} for call in attempts)


def test_rpc_execution_errors_are_never_retried():
    from scripts.transport import install_rpc_read_retries
    attempts = []

    def request(**kwargs):
        attempts.append(kwargs)
        raise GenLayerError('contract execution failed')

    client = install_rpc_read_retries(SimpleNamespace(provider=SimpleNamespace(make_request=request)))
    with pytest.raises(GenLayerError, match='execution failed'):
        client.provider.make_request(method='gen_call', params=[])
    assert len(attempts) == 1
