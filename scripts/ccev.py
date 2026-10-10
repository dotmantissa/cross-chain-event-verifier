#!/usr/bin/env python3
"""Typed JSON CLI for deployment and contract interaction.

Run from the repository root. Keys load from .env/environment, never arguments.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from genlayer_py import create_account, create_client
from genlayer_py.chains import localnet, studionet, testnet_bradbury
from genlayer_py.types import CalldataAddress, TransactionHashVariant, TransactionStatus
from gltest.assertions import tx_execution_succeeded
from gltest.utils import extract_contract_address

if __package__:
    from .transport import install_rpc_read_retries, install_studionet_read_compat
else:
    from transport import install_rpc_read_retries, install_studionet_read_compat


CHAINS = {"studionet": studionet, "localnet": localnet, "testnet-bradbury": testnet_bradbury}


def convert_arguments(value):
    """Explicit address tags preserve string hashes/chain IDs and ABI types."""
    if isinstance(value, dict):
        if set(value) == {"$address"}:
            return CalldataAddress(value["$address"])
        return {key: convert_arguments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [convert_arguments(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=CHAINS, default="studionet")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("call", "write"):
        sub = commands.add_parser(command)
        sub.add_argument("address")
        sub.add_argument("method")
        sub.add_argument("--args-json", default="[]", help="JSON array preserving string argument types")
    call = commands.choices["call"]
    call.add_argument("--state", choices=("final", "non-final"), default="final")
    deploy = commands.add_parser("deploy")
    deploy.add_argument("contract", type=Path)
    deploy.add_argument("--args-json", default="[]")
    deploy.add_argument("--checkpoint", type=Path, required=True)
    receipt_command = commands.add_parser("receipt")
    receipt_command.add_argument("transaction_hash")
    options = parser.parse_args()
    args = json.loads(getattr(options, "args_json", "[]"))
    if not isinstance(args, list):
        parser.error("--args-json must be an array")
    raw_args = args
    args = convert_arguments(raw_args)
    load_dotenv(".env")
    key = os.environ.get("CCEV_DEPLOYER_KEY")
    if options.command in ("deploy", "write") and not key:
        parser.error("Set CCEV_DEPLOYER_KEY in .env or the environment")
    # The read RPC requires a sender address even though it does not sign.
    account = create_account(key) if key else create_account()
    client = install_rpc_read_retries(install_studionet_read_compat(
        create_client(chain=CHAINS[options.network], account=account)))
    if options.command == "call":
        variant = TransactionHashVariant.LATEST_FINAL if options.state == "final" else TransactionHashVariant.LATEST_NONFINAL
        result = client.read_contract(address=options.address, function_name=options.method,
            args=args, transaction_hash_variant=variant)
        print(json.dumps(result, indent=2, default=str))
        return
    if options.command == "deploy":
        code = options.contract.read_text()
        fingerprint = hashlib.sha256(code.encode()).hexdigest()
        checkpoint = json.loads(options.checkpoint.read_text()) if options.checkpoint.exists() else {}
        if (checkpoint.get("network") == options.network
            and checkpoint.get("source_sha256") == fingerprint and checkpoint.get("args") == raw_args):
            tx_hash = checkpoint["transaction_hash"]
        else:
            tx_hash = client.deploy_contract(code=code, args=args)
            checkpoint = {"network": options.network, "contract": str(options.contract), "args": raw_args,
                "source_sha256": fingerprint, "transaction_hash": str(tx_hash), "status": "submitted"}
            options.checkpoint.parent.mkdir(parents=True, exist_ok=True)
            write_checkpoint(options.checkpoint, checkpoint)
    elif options.command == "write":
        tx_hash = client.write_contract(address=options.address, function_name=options.method, args=args)
    else:
        tx_hash = options.transaction_hash
    print(f"Transaction: {tx_hash}", flush=True)
    receipt = client.wait_for_transaction_receipt(transaction_hash=tx_hash,
        status=TransactionStatus.FINALIZED, interval=10000, retries=90)
    success = tx_execution_succeeded(receipt)
    if options.command == "deploy":
        checkpoint.update(status=str(receipt.get("status_name", receipt.get("status", ""))), execution_succeeded=success)
        if success:
            checkpoint["address"] = str(extract_contract_address(receipt))
        write_checkpoint(options.checkpoint, checkpoint)
    if not success:
        print(json.dumps(receipt, indent=2, default=str))
        raise SystemExit("Contract execution failed")
    print(json.dumps({"transaction_hash": str(tx_hash), "status": str(receipt.get("status_name", receipt.get("status", ""))),
        "execution_succeeded": True,
        **({"address": checkpoint["address"]} if options.command == "deploy" else {})}, indent=2))


def write_checkpoint(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


if __name__ == "__main__":
    main()
