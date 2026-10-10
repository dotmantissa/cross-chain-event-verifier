"""Shared fixtures for the Cross-Chain Event Verifier direct-mode suite.

Mock payloads here are shaped from real responses captured from live endpoints
(Ethereum JSON-RPC, Blockscout v2, Solana JSON-RPC), so a test that passes here
is exercising the same parsing path that production data takes.

Test sources use the reserved ``.invalid`` TLD (RFC 2606), which can never
resolve. A mock that fails to match therefore produces a clear error rather
than a real network call.
"""

import json
import sys

import pytest

CONTRACT = "contracts/cross_chain_event_verifier.py"
CONSUMER = "contracts/examples/cross_chain_release_gate.py"
MODULE_NAME = "_contract_cross_chain_event_verifier"

# A synthetic chain used by most tests so sources are fully controllable.
TEST_CHAIN = "99999"
TEST_CHAIN_HEX = hex(99999)

ALPHA = "https://alpha-rpc.test.invalid/rpc"
BETA = "https://beta-rpc.test.invalid/rpc"
GAMMA = "https://gamma-scout.test.invalid"
DELTA = "https://delta-rpc.test.invalid/rpc"

TEST_SOURCES = [
    {"label": "alpha", "kind": "evm_jsonrpc", "url": ALPHA},
    {"label": "beta", "kind": "evm_jsonrpc", "url": BETA},
    {"label": "gamma", "kind": "blockscout_v2", "url": GAMMA},
]

# Real values from Ethereum mainnet block 16777216. Kept verbatim so the
# fixtures stay anchored to data that genuinely exists on chain.
TX_HASH = "0x2bb8f36504a329b8d8eb4031a895aa952e1007c24843a531d3bf29093872f86a"
SENDER = "0x23f4569002a5a07f0ecf688142eeb6bcd883eef8"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
RECIPIENT = "0x09b6b873f9a7e0f4dcb2f4aadb6be39912aa1b30"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BLOCK = 16777216
TX_INDEX = 24
NONCE = 369105
AMOUNT = 100000000  # 100 USDT, 6 decimals

OTHER_TX = "0xb0b31990a5b94de6563f0ed84004a4eadb89a44a37a3ddc19f693235782b8aed"
UNKNOWN_TX = "0x" + "ab" * 32


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def topic_pad(address: str) -> str:
    """Left-pad an address into a 32 byte topic word."""
    return "0x" + "0" * 24 + address[2:].lower()


def word(value: int) -> str:
    return format(value, "064x")


def erc20_transfer_log(
    token: str = USDT,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
    amount: int = AMOUNT,
) -> dict:
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, topic_pad(sender), topic_pad(recipient)],
        "data": "0x" + word(amount),
        "logIndex": "0x0",
    }


def erc721_transfer_log(
    collection: str = "0xb47e3cd837ddf8e4c57f05d70ab865de6e193bbb",
    sender: str = SENDER,
    recipient: str = RECIPIENT,
    token_id: int = 7804,
) -> dict:
    return {
        "address": collection,
        "topics": [
            TRANSFER_TOPIC,
            topic_pad(sender),
            topic_pad(recipient),
            "0x" + word(token_id),
        ],
        "data": "0x",
        "logIndex": "0x1",
    }


def rpc_transaction(
    tx_hash: str = TX_HASH,
    sender: str = SENDER,
    to: str = USDT,
    value: int = 0,
    block: int = BLOCK,
    nonce: int = NONCE,
    input_data: str = "0xa9059cbb",
) -> dict:
    """An eth_getTransactionByHash result. ``block=None`` means still pending."""
    return {
        "hash": tx_hash,
        "from": sender,
        "to": to,
        "value": hex(value),
        "nonce": hex(nonce),
        "input": input_data,
        "blockNumber": hex(block) if block is not None else None,
        "blockHash": "0x" + "11" * 32 if block is not None else None,
        "transactionIndex": hex(TX_INDEX) if block is not None else None,
        "gas": "0x15f90",
        "gasPrice": "0x4a817c800",
        "type": "0x2",
    }


def rpc_receipt(
    tx_hash: str = TX_HASH,
    sender: str = SENDER,
    to: str = USDT,
    block: int = BLOCK,
    status: int = 1,
    logs: list | None = None,
    contract_address: str | None = None,
) -> dict:
    """An eth_getTransactionReceipt result."""
    return {
        "transactionHash": tx_hash,
        "from": sender,
        "to": to,
        "blockNumber": hex(block),
        "blockHash": "0x" + "11" * 32,
        "transactionIndex": hex(TX_INDEX),
        "status": hex(status),
        "gasUsed": "0xfd50",
        "cumulativeGasUsed": "0x1b2c3d",
        "effectiveGasPrice": "0x4a817c800",
        "contractAddress": contract_address,
        "logs": logs if logs is not None else [erc20_transfer_log()],
        "logsBloom": "0x" + "00" * 256,
        "type": "0x2",
    }


def evm_batch(
    transaction: dict | None,
    receipt: dict | None,
    head: int,
    chain_id_hex: str = TEST_CHAIN_HEX,
) -> str:
    """The batched JSON-RPC reply the evm_jsonrpc adapter sends.

    Request ids are fixed by the adapter: 1 chainId, 2 transaction,
    3 receipt, 4 blockNumber.
    """
    return json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "result": chain_id_hex},
            {"jsonrpc": "2.0", "id": 2, "result": transaction},
            {"jsonrpc": "2.0", "id": 3, "result": receipt},
            {"jsonrpc": "2.0", "id": 4, "result": hex(head)},
        ]
    )


def blockscout_transaction(
    tx_hash: str = TX_HASH,
    sender: str = SENDER,
    to: str = USDT,
    value: int = 0,
    block: int | None = BLOCK,
    confirmations: int = 64,
    result: str = "success",
    status: str = "ok",
    nonce: int = NONCE,
    raw_input: str = "0xa9059cbb",
) -> str:
    """A Blockscout v2 /transactions/{hash} payload.

    Blockscout reports checksum-cased addresses and a decimal value, which is
    exactly the formatting difference the normaliser exists to absorb.
    """
    return json.dumps(
        {
            "hash": tx_hash,
            "from": {"hash": sender.upper().replace("0X", "0x")},
            "to": {"hash": to.upper().replace("0X", "0x")},
            "value": str(value),
            "nonce": nonce,
            "raw_input": raw_input,
            "block_number": block,
            "position": TX_INDEX,
            "confirmations": confirmations,
            "result": result,
            "status": status,
            "method": "transfer",
            "gas_used": "64848",
            "gas_price": "20000000000",
            "created_contract": None,
            "timestamp": "2023-02-16T12:00:00.000000Z",
        }
    )


def blockscout_logs(logs: list | None = None) -> str:
    """A Blockscout v2 /transactions/{hash}/logs payload.

    Blockscout pads the topics array to four entries with nulls, which the
    adapter has to strip.
    """
    source = logs if logs is not None else [erc20_transfer_log()]
    items = []
    for index, log in enumerate(source):
        topics = list(log["topics"])
        while len(topics) < 4:
            topics.append(None)
        items.append(
            {
                "address": {"hash": log["address"].upper().replace("0X", "0x")},
                "topics": topics,
                "data": log["data"],
                "index": index,
                "block_number": BLOCK,
                "transaction_hash": TX_HASH,
                "decoded": None,
            }
        )
    return json.dumps({"items": items, "next_page_params": None})


def solana_batch(transaction: dict | None, slot_head: int) -> str:
    return json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "result": transaction},
            {"jsonrpc": "2.0", "id": 2, "result": slot_head},
        ]
    )


def solana_transaction(
    slot: int = 455089019,
    err: object = None,
    accounts: list | None = None,
    logs: list | None = None,
) -> dict:
    keys = accounts or [
        "SA1LFXr4os2P4VKGUyRv84uFfuUYgcQkFh2uA4SmRcr",
        "Sa1HXZsn2u6p2dMLZGhfxtsRw7Jo32hF15yBghWJsCz",
        "Vote111111111111111111111111111111111111111",
    ]
    return {
        "slot": slot,
        "blockTime": 1791599023,
        "transactionIndex": 3,
        "version": 0,
        "meta": {
            "err": err,
            "fee": 5000,
            "status": {"Ok": None} if err is None else {"Err": err},
            "logMessages": logs
            if logs is not None
            else [
                "Program Vote111111111111111111111111111111111111111 invoke [1]",
                "Program Vote111111111111111111111111111111111111111 success",
            ],
            "preBalances": [100, 200, 0],
            "postBalances": [95, 200, 0],
            "computeUnitsConsumed": 2100,
        },
        "transaction": {
            "message": {
                "accountKeys": keys,
                "instructions": [{"programIdIndex": 2, "accounts": [0, 1], "data": "AQ=="}],
            },
            "signatures": ["sig" + "1" * 40],
        },
    }


# ---------------------------------------------------------------------------
# Mock registration helpers
# ---------------------------------------------------------------------------


def mock_rpc(direct_vm, url: str, body: str, status: int = 200) -> None:
    """Mock one evm_jsonrpc or solana_jsonrpc source (a POST)."""
    import re

    direct_vm.mock_web(
        re.escape(url), {"method": "POST", "status": status, "body": body}
    )


def mock_blockscout(
    direct_vm,
    base: str,
    tx_hash: str,
    detail_body: str,
    logs_body: str | None = None,
    status: int = 200,
) -> None:
    """Mock a blockscout_v2 source. Logs pattern is registered first so the
    more specific URL wins, since mocks match in registration order."""
    import re

    if logs_body is not None:
        direct_vm.mock_web(
            re.escape(f"{base}/api/v2/transactions/{tx_hash}/logs"),
            {"method": "GET", "status": status, "body": logs_body},
        )
    direct_vm.mock_web(
        re.escape(f"{base}/api/v2/transactions/{tx_hash}") + r"$",
        {"method": "GET", "status": status, "body": detail_body},
    )


def mock_all_agree(direct_vm, head: int = BLOCK + 64, tx_hash: str = TX_HASH) -> None:
    """Three sources that all report the same confirmed transaction."""
    transaction = rpc_transaction(tx_hash=tx_hash)
    receipt = rpc_receipt(tx_hash=tx_hash)
    body = evm_batch(transaction, receipt, head)
    mock_rpc(direct_vm, ALPHA, body)
    mock_rpc(direct_vm, BETA, body)
    mock_blockscout(
        direct_vm,
        GAMMA,
        tx_hash,
        blockscout_transaction(tx_hash=tx_hash, confirmations=head - BLOCK + 1),
        blockscout_logs(),
    )


def mock_llm_verdict(
    direct_vm, verdict: str = "match", confidence: int = 95, reasoning: str = "Matches."
) -> None:
    direct_vm.mock_llm(
        r"verifying whether a natural-language claim",
        json.dumps(
            {"verdict": verdict, "confidence": confidence, "reasoning": reasoning}
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mod(verifier):
    """The loaded contract module, for unit-testing its pure functions.

    The direct-mode loader registers the contract under a stable name in
    ``sys.modules``, so every module-level helper is reachable by name. This is
    what lets the equivalence and aggregation logic be tested directly rather
    than only through mocked network paths.
    """
    return sys.modules[MODULE_NAME]


def addr_hex(value) -> str:
    """Render any address form the harness hands back as lowercase hex.

    ``create_address`` returns a genlayer ``Address`` once the SDK is loaded and
    raw bytes before that, and which one a test sees depends on whether the
    fixture resolved before or after the first deploy. Normalising here keeps
    tests independent of that ordering.
    """
    if hasattr(value, "as_hex"):
        return value.as_hex.lower()
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value).lower()


@pytest.fixture
def at_time(direct_vm):
    """Set the deterministic clock the contract reads.

    ``direct_vm.warp()`` alone is not sufficient here. It stores the timestamp
    and calls the harness's message refresh, but that refresh only propagates
    ``sender_address`` and ``origin_address`` into ``gl.message_raw``; the
    ``datetime`` key keeps whatever value was injected when the contract was
    loaded. Since the contract's clock is ``gl.message_raw["datetime"]`` (the
    only clock that is identical across the leader and every validator), tests
    have to write that key directly.
    """

    def _set(iso: str) -> None:
        direct_vm.warp(iso)
        gl_module = sys.modules.get("genlayer.gl")
        if gl_module is not None and getattr(gl_module, "message_raw", None) is not None:
            gl_module.message_raw["datetime"] = iso

    return _set


@pytest.fixture
def verifier(direct_vm, direct_deploy, direct_owner):
    """A verifier with no default chains, carrying only the synthetic test chain."""
    direct_vm.sender = direct_owner
    contract = direct_deploy(CONTRACT, register_defaults=False)
    contract.register_chain(
        TEST_CHAIN,
        "Test Chain",
        "evm",
        99999,
        12,
        10,
        2,
        json.dumps(TEST_SOURCES),
    )
    return contract


@pytest.fixture
def verifier_with_defaults(direct_vm, direct_deploy, direct_owner):
    """A verifier carrying the shipped default chain registry."""
    direct_vm.sender = direct_owner
    return direct_deploy(CONTRACT, register_defaults=True)
