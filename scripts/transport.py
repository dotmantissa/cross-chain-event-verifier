"""StudioNet gen_call transport compatibility for genlayer-py 0.16.3.

Hosted StudioNet accepts raw GenLayer calldata for read requests. Its legacy
RLP-envelope decoder fails on long argument payloads, although the SDK's RLP
encoding is valid. Use raw calldata on that host; writes keep SDK signing and
transaction serialization unchanged.
"""
from types import MethodType
import time
from urllib.parse import urlparse

import requests

from genlayer_py.abi import calldata
from genlayer_py.contracts.utils import make_calldata_object
from genlayer_py.exceptions import GenLayerError
from genlayer_py.types import TransactionHashVariant


READ_RPC_METHODS = frozenset({"eth_chainId", "eth_getBalance", "eth_estimateGas",
    "eth_getTransactionCount", "eth_getTransactionByHash", "eth_getTransactionReceipt",
    "eth_blockNumber", "gen_call", "gen_getContractCode", "gen_getContractSchema"})


def install_rpc_read_retries(client):
    """Retry transport failures on reads; never resubmit transactions."""
    provider = client.provider
    if getattr(provider, "_ccev_read_retries_installed", False):
        return client
    original = provider.make_request

    def request(method, params):
        for attempt in range(3):
            try:
                return original(method=method, params=params)
            except GenLayerError as error:
                if (method not in READ_RPC_METHODS or attempt == 2
                    or not isinstance(error.__cause__, requests.exceptions.RequestException)):
                    raise
                time.sleep(2 ** attempt)

    provider.make_request = request
    provider._ccev_read_retries_installed = True
    return client


def _read_contract(
    self, address, function_name, args=None, kwargs=None, account=None,
    raw_return=False,
    transaction_hash_variant=TransactionHashVariant.LATEST_NONFINAL,
    sim_config=None,
):
    sender = account if account is not None else self.local_account
    if sender is None:
        raise GenLayerError("No account provided and no account is connected")
    encoded = calldata.encode(make_calldata_object(method=function_name, args=args, kwargs=kwargs))
    request = {
        "type": "read", "to": address, "from": sender.address,
        "data": "0x" + encoded.hex(),
        "transaction_hash_variant": transaction_hash_variant.value,
    }
    if sim_config is not None:
        request["sim_config"] = sim_config
    result = self.provider.make_request(method="gen_call", params=[request])["result"]
    hexadecimal = result.removeprefix("0x")
    return "0x" + hexadecimal if raw_return else calldata.decode(bytes.fromhex(hexadecimal))


def install_studionet_read_compat(client):
    """Apply only to the affected hosted endpoint, never to unrelated chains."""
    if urlparse(client.provider.url).hostname == "studio.genlayer.com":
        client.read_contract = MethodType(_read_contract, client)
    return client
