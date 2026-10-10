"""Shared fixtures for the integration suite.

These tests run against a real GenLayer network with real consensus, and they
query real public block explorers. Nothing here is mocked.

Deployments are session scoped on purpose. studio.genlayer.com is shared and
rate limited to 60 requests per minute, and every consensus round costs several
requests, so redeploying per test would trip the limit rather than prove
anything extra.

The fixtures exist to cover the one thing direct mode cannot: the GenVM SDK
permits only one contract class per process, so the verifier and the release
gate can never both be loaded in a direct-mode run. Here each contract is
genuinely deployed and the cross-contract read goes over the real network.
"""

import functools
import hashlib
import json
import os
from pathlib import Path

import pytest
from genlayer_py.types import CalldataAddress, TransactionStatus
from gltest import get_contract_factory
from gltest.clients import get_gl_client
from gltest.utils import extract_contract_address
from gltest_cli.config.general import get_general_config
from scripts.transport import install_studionet_read_compat
from gltest.assertions import tx_execution_succeeded

# Ethereum mainnet, block 16777216. A USDT transfer, finalised years ago, so
# its facts can never change. Every value below was read from live archive RPC
# (eth.drpc.org and eth-mainnet.public.blastapi.io agreed on all of them).
ETH = "1"
TX = "0x2bb8f36504a329b8d8eb4031a895aa952e1007c24843a531d3bf29093872f86a"
TX_SENDER = "0x23f4569002a5a07f0ecf688142eeb6bcd883eef8"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
TX_RECIPIENT = "0x09b6b873f9a7e0f4dcb2f4aadb6be39912aa1b30"
TX_BLOCK = 16777216
TX_INDEX = 24
TX_NONCE = 369105
TX_SELECTOR = "0xa9059cbb"
TRANSFER_AMOUNT = 100000000
TRANSFER_SIG = "Transfer(address,address,uint256)"

# A syntactically valid hash that has never existed on Ethereum.
ABSENT_TX = "0x" + "ab" * 32

LONG_WAIT = {"wait_interval": 10000, "wait_retries": 90}


def topic_pad(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


def word(value: int) -> str:
    return "0x" + format(value, "064x")


def transfer_assertion(
    recipient: str = TX_RECIPIENT, amount: int = TRANSFER_AMOUNT
) -> str:
    """An assertion pinning the exact USDT transfer in the fixture transaction."""
    return json.dumps(
        {
            "address": USDT,
            "signature": TRANSFER_SIG,
            "topics": {"1": TX_SENDER, "2": recipient},
            "data_contains": word(amount),
        }
    )


def journal_receipts(client):
    """Record public execution evidence without changing RPC or test results."""
    if getattr(client, "_ccev_journal_installed", False):
        return
    original = client.wait_for_transaction_receipt

    @functools.wraps(original)
    def wait(*args, **kwargs):
        tx_hash = str(kwargs.get("transaction_hash", args[0] if args else ""))
        path = Path(os.getenv("CCEV_EVIDENCE_FILE", "deployments/studionet.evidence.pending.json"))
        records = json.loads(path.read_text()) if path.exists() else {}
        if tx_hash and tx_hash not in records:
            records[tx_hash] = {"transaction_hash": tx_hash, "status": "submitted",
                "execution_result": "", "votes": {}}
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(records, indent=2) + "\n")
            temporary.replace(path)
        print(f"Waiting for transaction {tx_hash}", flush=True)
        receipt = original(*args, **kwargs)
        tx_hash = tx_hash or str(receipt.get("hash", ""))
        consensus = receipt.get("consensus_data", {})
        leaders = consensus.get("leader_receipt", [])
        execution = leaders[0].get("execution_result", "") if leaders else ""
        evidence = {"transaction_hash": tx_hash, "status": str(receipt.get("status_name", receipt.get("status", ""))),
            "execution_result": execution, "votes": consensus.get("votes", {})}
        records = json.loads(path.read_text()) if path.exists() else {}
        records[tx_hash] = evidence
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, indent=2, default=str) + "\n")
        temporary.replace(path)
        print(f"Transaction {tx_hash}: {evidence['status']} / {execution}", flush=True)
        return receipt

    client.wait_for_transaction_receipt = wait
    client._ccev_journal_installed = True


def deploy_checkpointed(name, args):
    """Persist the transaction ID before polling so interrupted runs can resume.

    Reuse requires matching network, source SHA-256 and constructor arguments.
    A failed deployment remains visible in the checkpoint and is never treated
    as a deployed contract.
    """
    factory = get_contract_factory(name)
    client = install_studionet_read_compat(get_gl_client())
    journal_receipts(client)
    checkpoint = Path(os.getenv("CCEV_DEPLOYMENTS_FILE", "deployments/studionet.pending.json"))
    fingerprint = hashlib.sha256(factory.contract_code.encode()).hexdigest()
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {"contracts": {}}
    network = get_general_config().get_rpc_url()
    if state.get("rpc_url", network) != network:
        raise ValueError("Deployment checkpoint belongs to a different network")
    state["rpc_url"] = network
    previous = state["contracts"].get(name, {})
    argument_encoding = "calldata-address-v1" if name == "CrossChainReleaseGate" else "json-v1"
    if (previous.get("source_sha256") == fingerprint and previous.get("args") == args
        and previous.get("argument_encoding", "json-v1") == argument_encoding):
        tx_hash = previous["transaction_hash"]
    else:
        typed_args = [CalldataAddress(args[0]), *args[1:]] if name == "CrossChainReleaseGate" else args
        tx_hash = client.deploy_contract(code=factory.contract_code, args=typed_args)
        state["contracts"][name] = {
            "source_sha256": fingerprint, "args": args, "argument_encoding": argument_encoding,
            "transaction_hash": str(tx_hash), "status": "submitted",
        }
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        temporary.replace(checkpoint)
    print(f"{name} deploy transaction: {tx_hash}", flush=True)
    receipt = client.wait_for_transaction_receipt(
        transaction_hash=tx_hash, status=TransactionStatus.FINALIZED,
        interval=LONG_WAIT["wait_interval"], retries=LONG_WAIT["wait_retries"],
    )
    assert tx_execution_succeeded(receipt), receipt
    address = str(extract_contract_address(receipt))
    state["contracts"][name].update(address=address, status="finalized", execution_result="SUCCESS")
    temporary = checkpoint.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(checkpoint)
    return factory.build_contract(contract_address=address)


@pytest.fixture(scope="session")
def verifier():
    return deploy_checkpointed("CrossChainEventVerifier", [True])


@pytest.fixture(scope="session")
def verified_fixture_tx(verifier):
    """Run one real verification of the fixture transaction, once.

    Most tests only need to read the resulting record, and a single consensus
    round against live explorers is the expensive part. Waits for FINALIZED
    because the release gate reads the verifier against finalised state.
    """
    receipt = verifier.verify_transaction(args=[ETH, TX]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED, **LONG_WAIT
    )
    assert tx_execution_succeeded(receipt), receipt
    return verifier.get_verification(args=[ETH, TX]).call()


@pytest.fixture(scope="session")
def proven_fixture_event(verifier):
    """Prove the fixture transaction's USDT transfer, once, and finalise it.

    The release gate can only settle against a finalised proof, so this is the
    prerequisite for the consumer tests.
    """
    assertion = transfer_assertion()
    receipt = verifier.prove_event(args=[ETH, TX, assertion]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED, **LONG_WAIT
    )
    assert tx_execution_succeeded(receipt), receipt
    proof = verifier.get_event_proof(args=[ETH, TX, assertion]).call()
    assert proof["satisfied"] is True, proof
    return assertion


@pytest.fixture(scope="session")
def release_gate(verifier):
    """A release gate pointed at the deployed verifier."""
    return deploy_checkpointed("CrossChainReleaseGate", [str(verifier.address), True])
