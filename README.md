# Cross-Chain Event Verifier

A GenLayer intelligent contract for checking external transactions and events. Every validator independently fetches an owner-managed source registry, normalizes the evidence, and binds the authorization decision and supported facts before storage.

| Method | Result |
| --- | --- |
| `verify_transaction(chain_id, tx_hash)` | Existence, execution status, source quorum and application confirmation policy |
| `prove_event(chain_id, tx_hash, assertion)` | Structured receipt-log proof without an LLM |
| `verify_claim(chain_id, tx_hash, claim)` | Independent model adjudication against a consensus-bound evidence summary |
| `publish_to_gate(chain_id, tx_hash, assertion, gate)` | Authenticated evidence queued only on GenLayer finalization |

The [release gate](contracts/examples/cross_chain_release_gate.py) requires owner-approved conditions and finalized evidence delivery. A current verifier read can veto the delivered snapshot; provisional positive state cannot authorize release alone. It provides replay protection and pause controls, and records authorization without holding funds.

## Getting started

Python 3.14 and the pinned tooling are validated:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python scripts/prepare_direct_sdk.py
genvm-lint check contracts/cross_chain_event_verifier.py
genvm-lint typecheck contracts/cross_chain_event_verifier.py
genvm-lint check contracts/examples/cross_chain_release_gate.py
genvm-lint typecheck contracts/examples/cross_chain_release_gate.py
pytest -p no:gltest tests/direct -q
pytest -p no:gltest tests/gate -q
```

The SDK allows one contract class per direct-test process. Both suites exercise real storage/contract logic with controlled external boundaries. Saved validator callbacks independently rerun observations. Live integration tests establish actual protocol consensus, finalized parent/child message delivery, current-state reads and settlement:

```bash
cp .env.example .env
# Privately populate CCEV_DEPLOYER_KEY.
CCEV_DEPLOYMENTS_FILE=deployments/fresh-run.pending.json \
CCEV_EVIDENCE_FILE=deployments/fresh-run.evidence.pending.json \
pytest --network studionet tests/integration -v -s
```

Use a fresh gate checkpoint for a full lifecycle run. Follow journaled transaction IDs after interruption; an old settlement cannot replace a fresh test. StudioNet is gasless. See [operations](docs/OPERATIONS.md) for bounded transport, failed-deployment recovery and typed calls.

## Deployment and validation

The [deployment manifest](deployments/studionet.json) is the authoritative source for addresses, exact source hashes and finalized execution evidence. [Validation details](docs/VALIDATION.md) distinguish live coverage from controlled tests and historic deployments. Contracts are immutable; source changes require redeployment.

The registry ships Ethereum, Sepolia, Base, Arbitrum, OP, Polygon and Solana configurations. Live contract validation is explicitly reported per chain; configurations without published live evidence are experimental. Etherscan needs an API key and is not a default source.

## Trust and limits

This is a consensus oracle, not a cryptographic light client. Registry governance must select independent source operators. EVM depth-based finality is application policy, including on L2s; it is not an L1 withdrawal guarantee. Solana accounts/programs include version-0 loaded addresses, while SOL/SPL transfer amounts remain unknown. Generic program-log text does not establish full instruction semantics.

Natural-language claims can be indeterminate. Exact mutable confirmation counts, missing token decimals and off-chain context are unsupported claim facts. Refresh invalidates old claims; source-policy changes, pause, disablement and explicit revocation veto cached positives. Use exact structured criteria for consequential releases, and wait for both the delivery and gate settlement to finalize successfully.

[Architecture](docs/ARCHITECTURE.md) · [API](docs/API.md) · [Integration](docs/INTEGRATION.md) · [Operations](docs/OPERATIONS.md) · [Validation](docs/VALIDATION.md)
