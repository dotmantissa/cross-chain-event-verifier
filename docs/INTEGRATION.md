# Integrating the verifier

Submit a verification, check successful execution, wait for GenLayer finalization, then consume its cached result in a separate transaction. External-chain confirmation and GenLayer finalization are different checks.

## Reading from another intelligent contract

```python
from genlayer import *
from genlayer.py.public_abi import StorageType

# Inside a deterministic consumer method:
reader = gl.get_contract_at(Address(verifier_address)).view(
    state=StorageType.LATEST_FINAL
)
if not reader.is_verified_strict("1", tx_hash):
    raise gl.vm.UserError("Transaction is not strictly confirmed")
if not reader.is_event_proven("1", tx_hash, assertion_json):
    raise gl.vm.UserError("Required transfer has not been proven")
# Apply the consumer's own beneficiary, amount and replay rules here.
```

Cross-contract writes use `emit()` and are asynchronous. Emitting `prove_event` and immediately reading a proof in the same method does not wait for that verification. Arrange two transactions or a finalized callback workflow.

## Assertion format

`normalize_assertion` validates/canonicalizes JSON without a write. `prove_event` and all proof reads canonicalize the same way, so whitespace and key order do not change the proof key.

| Field | Meaning |
| --- | --- |
| `address` | Exact 20-byte EVM log emitter |
| `signature` | Canonical event signature; contract derives Keccak topic0 |
| `topic0` | Optional explicit 32-byte event topic; must match supplied signature |
| `topics` | Object mapping positions `1` through `3` to exact 32-byte values or addresses padded to 32 bytes |
| `data_contains` | Nonempty substring of the normalized log data, with optional `0x` prefix |
| `min_count` | Minimum number of matching logs; defaults to 1 |

At least one matching criterion is required. `data_contains` is a substring rule, not a general ABI-field equality decoder. For a standard ERC-20 Transfer, its single non-indexed amount occupies one 32-byte word; pin the emitter, signature, sender and recipient topics alongside that full amount word. For other ABI layouts, choose criteria that uniquely identify the intended event. For Solana, assertions can search program log text through `data_contains`; EVM address/topic criteria do not apply.

Event proofs never invoke an LLM. Known decoders distinguish ERC-20 versus ERC-721 Transfer by topic count. Use `known_event_signatures` and `decode_logs` to preview supported decoding. Unknown events remain queryable by raw topics/data.

## Release gate

Deploy `CrossChainReleaseGate(verifier_address, True)`. Its default authorization uses authenticated finalized messages and an exact current-state veto. This works without depending on the host's finalized inter-contract storage-read implementation.

1. The owner calls `create_condition(condition_id, chain_id, assertion, description, beneficiary, deadline)`. Canonical nonempty assertions bind event criteria. An empty assertion requires transaction confirmation only. Deadline 0 disables expiry.
2. Anyone calls `publish_to_gate(chain_id, tx_hash, assertion, gate_address)` on the verifier. This verifies/proves as needed and emits a snapshot with `on="finalized"`.
3. Follow the parent transaction to `FINALIZED` with successful execution, then obtain the emitted child ID with `get_triggered_transaction_ids` and wait for its successful finalization. Parent finality alone does not establish delivery.
4. Preview `check_readiness`, then call `settle`. Current verifier evidence must exactly match the delivered snapshot. If refreshed, revoked, expired or invalidated by policy, refresh/reprove and publish again.
5. Downstream consumers wait for this gate settlement's successful finalization before irreversible effects.

The current-state read uses `LATEST_NON_FINAL` solely to veto old delivered evidence; positive provisional state cannot independently authorize release. Event conditions also enforce strict confirmation unless the owner explicitly changes strict policy. A transaction settles at most one condition per chain in a gate. EVM hash casing normalizes; Solana signature casing is preserved. Migration to a new gate requires application replay-history preservation.

Only owners register conditions, preventing arbitrary sponsors from squatting IDs or consuming unrelated deposits. Sponsor/owner cancellation is supported. Owner pause vetoes new conditions and settlement. Switching verifiers namespaces away old snapshots and rejects the old verifier's messages. Conditions retain their beneficiary; settlement caller is recorded separately. The contract holds no funds.

Bind chain, emitter, party addresses, amount and beneficiary before settlement. A transaction-only condition imposes no payment requirements. `data_contains` is a substring match: verify that the selected word and event layout uniquely identify the intended amount.

## Natural-language claims

`verify_claim` independently runs a model on each validator against the normalized transaction and decoded summaries. Verdicts are `match`, `mismatch` or `indeterminate`. The model cannot establish transaction existence or confirmation. `is_claim_verified` additionally requires a confirmed, fresh transaction under current policy and the exact adjudicated transaction epoch. Every refresh invalidates earlier claims. Mutable exact confirmation counts and unsupported SOL/SPL transfer amounts cannot establish claims.

Use exact base units and explicit addresses in claims. Human-readable token amounts can require decimals or context the verifier does not fetch. Model confidence is an explanatory score, not a cryptographic probability. Prefer structured assertions for consequential releases.

## Off-chain typed calls

The helper preserves numeric-looking strings and long JSON assertions:

```bash
python3 scripts/ccev.py call VERIFIER is_verified_strict \
  --args-json '["1","0xYOUR_64_HEX_CHARACTER_HASH"]'
python3 scripts/ccev.py write VERIFIER prove_event \
  --args-json '["1","0xYOUR_64_HEX_CHARACTER_HASH","{\"address\":\"0xYOUR_TOKEN\"}"]'
```

Replace placeholders with valid data. The helper reads finalized state by default and waits for finalized successful writes. StudioNet read requests use raw calldata because its legacy RLP read decoder rejects some long arguments. The installed `genlayer` CLI auto-coerces numeric/hex arguments; use the helper for string chain IDs, hashes and JSON assertion strings.

Publish a proof to a gate with the typed helper:

```bash
python3 scripts/ccev.py write VERIFIER publish_to_gate \
  --args-json '["1","0xYOUR_64_HEX_CHARACTER_HASH","{\"address\":\"0xYOUR_TOKEN\"}",{"$address":"0xYOUR_GATE"}]'
```

Follow the child transaction as described above before settlement.
