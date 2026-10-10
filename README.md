# Cross-Chain Event Verifier

A GenLayer intelligent contract for checking transactions and events on another chain. Each validator fetches independent evidence from an owner-managed registry of public sources, normalizes it, and checks the leader's result before the result is stored on-chain.

The primitive offers three paths:

| Write method | Evidence and result |
| --- | --- |
| `verify_transaction(chain_id, tx_hash)` | Transaction existence, execution status, source quorum and confirmation depth |
| `prove_event(chain_id, tx_hash, assertion)` | An exact structured assertion matched against receipt logs, with no LLM |
| `verify_claim(chain_id, tx_hash, claim)` | A natural-language claim adjudicated against code-derived facts by independently running validators |

For value-sensitive consumers, use structured event proofs and `is_verified_strict` against finalized GenLayer state. The [Cross-Chain Release Gate](contracts/examples/cross_chain_release_gate.py) demonstrates the two-step verification and settlement flow with replay protection.

## Getting started

Python 3.14 is the environment validated for this project. Install the pinned tooling:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt

genvm-lint check contracts/cross_chain_event_verifier.py
genvm-lint typecheck contracts/cross_chain_event_verifier.py
genvm-lint check contracts/examples/cross_chain_release_gate.py
genvm-lint typecheck contracts/examples/cross_chain_release_gate.py
pytest tests/direct -q
```

Direct tests use in-memory VM storage and mocked source/LLM responses. The equivalence tests additionally run the saved validator callback against independent observations. Real VM execution and cross-contract reads are covered by the integration suite:

```bash
cp .env.example .env
# Set CCEV_DEPLOYER_KEY in .env using your own deployer account.
gltest tests/integration -v -s --network studionet
```

StudioNet is gasless. The live suite deploys contracts, sends transactions, queries public Ethereum sources, and runs LLM adjudication. Run it separately from the direct suite. Deployments are checkpointed before polling in `deployments/studionet.pending.json`; matching source and constructor arguments reuse that deployment on the next run. Use `CCEV_DEPLOYMENTS_FILE` to choose a different checkpoint path.

## Prove a transfer

The assertion uses raw base units, exact token and party addresses, an event signature, and a 32-byte ABI data word:

```python
import json

assertion = json.dumps({
    "address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "signature": "Transfer(address,address,uint256)",
    "topics": {
        "1": "0x23f4569002a5a07f0ecf688142eeb6bcd883eef8",
        "2": "0x09b6b873f9a7e0f4dcb2f4aadb6be39912aa1b30",
    },
    "data_contains": "0x" + format(100000000, "064x"),
    "min_count": 1,
})

# Through genlayer-test's contract wrapper:
receipt = verifier.prove_event(args=["1", tx_hash, assertion]).transact()
# Check execution success, wait for FINALIZED, then read the proof.
```

`get_event_proof(...)["valid"]` and `is_event_proven(...)` include freshness and current transaction evidence checks. A historical `satisfied` field alone is insufficient for settlement.

## Default chains

| Registry ID | Chain | Minimum confirmations | Drift band | Source quorum |
| --- | --- | ---: | ---: | ---: |
| `1` | Ethereum mainnet | 12 | 10 | 2 |
| `11155111` | Ethereum Sepolia | 6 | 10 | 2 |
| `8453` | Base | 60 | 60 | 2 |
| `42161` | Arbitrum One | 480 | 480 | 2 |
| `10` | OP Mainnet | 60 | 60 | 2 |
| `137` | Polygon PoS | 128 | 60 | 2 |
| `solana-mainnet` | Solana mainnet beta | 32 slots | 400 | 2 |

These are application confirmation policies. The EVM `final` label means the configured depth was reached; it is not a cryptographic proof of protocol finality or an L2 withdrawal guarantee. Solana requests finalized commitment. See [architecture and assumptions](docs/ARCHITECTURE.md).

## Builder documentation

- [Architecture and trust boundary](docs/ARCHITECTURE.md)
- [Consumer integration, assertion format and release gate](docs/INTEGRATION.md)
- [API reference](docs/API.md)
- [Testing, deployment and recovery](docs/OPERATIONS.md)

The contracts pin a concrete GenVM runner version. They are immutable deployments; changing source requires deployment of a new verifier/gate and an explicit consumer migration. Hosted-source availability and GenLayer appeals/finality remain part of the integration contract.
