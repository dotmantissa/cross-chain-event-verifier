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

Deploy `CrossChainReleaseGate(verifier_address, True)`. Strict confirmation is enabled, and verifier reads use `LATEST_FINAL` by default.

1. A sponsor calls `create_condition(condition_id, chain_id, assertion, description, beneficiary, deadline)`. The verifier canonicalizes a nonempty assertion. An empty assertion requires transaction confirmation only. A deadline of 0 disables expiry.
2. Anyone calls the verifier's `prove_event` or `verify_transaction` and waits for successful finalized execution.
3. Anyone can preview `check_readiness(condition_id, tx_hash)` and call `settle(condition_id, tx_hash)`.
4. Downstream contracts read `is_settled(condition_id)` from finalized gate state before acting.

An event condition requires both its exact proof and strict confirmation when `require_strict` is enabled. A transaction settles at most one condition per chain in a given gate. EVM hashes normalize casing and prefix; Solana signatures preserve base58 casing. Replay protection is scoped to the gate deployment, so a new gate or a separate application must deliberately preserve its own consumed-deposit history.

Sponsors or the gate owner can cancel unsettled conditions. Only the owner can change verifier address, strict policy or non-final-read policy. Conditions retain the beneficiary selected at creation; `settle` records the submitting caller separately. The contract does not transfer assets.

For escrow/deposit applications, bind a condition to the expected beneficiary and transfer criteria before settlement. A condition requiring only transaction existence establishes no payment amount, token or recipient requirement.

## Natural-language claims

`verify_claim` independently runs a model on each validator against the normalized transaction and decoded summaries. Verdicts are `match`, `mismatch` or `indeterminate`. The model cannot establish transaction existence or confirmation. `is_claim_verified` additionally requires a confirmed, fresh current transaction record.

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
