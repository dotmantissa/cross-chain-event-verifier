# API reference

All chain IDs and transaction identifiers are strings. EVM hashes require 32 bytes with a `0x` prefix and normalize casing. Solana identifiers are base58 strings preserving case. Structured assertions and source lists are JSON **strings**, not calldata dictionaries.

## Verifier writes

| Method | Access | Result |
| --- | --- | --- |
| `verify_transaction(chain_id, tx_hash)` | Permissionless unless restricted | Fresh cached transaction view, or new consensus record |
| `publish_to_gate(chain_id, tx_hash, assertion, gate: Address)` | Same submission policy | Verifies/proves if needed, queues authenticated evidence on finalization |
| `refresh_transaction(chain_id, tx_hash)` | Same submission policy | Forces fresh consensus, increments transaction epoch |
| `prove_event(chain_id, tx_hash, assertion)` | Same submission policy | Structured proof view and transaction view on a new proof |
| `verify_claim(chain_id, tx_hash, claim)` | Same submission policy | Claim verdict and transaction view on a new adjudication; claim max 2,000 chars |

Cache hits do not necessarily include the nested transaction view that a new proof/claim write returns. Read `get_verification` explicitly when required. A successful write can store an absent, pending, failed, disputed or mismatching result; execution success is distinct from verification success.

## Verifier reads

| Method | Meaning |
| --- | --- |
| `is_verified(chain_id, tx_hash)` | Confirmed, unexpired, unrevoked and under the current enabled/unpaused policy |
| `is_verified_strict(chain_id, tx_hash)` | Also outside the confirmation uncertainty band |
| `get_release_evidence(chain_id, tx_hash, assertion)` | Stable snapshot for finalized delivery and current veto |
| `get_verification(chain_id, tx_hash)` | Transaction facts, evidence counts, source reports, decoded summaries and cache metadata |
| `record_status(chain_id, tx_hash)` | `unverified`, `expired`, `success`, `failed`, `pending`, `absent` or `disputed` |
| `is_event_proven(chain_id, tx_hash, assertion)` | Satisfied, unexpired proof consistent with current confirmed transaction evidence |
| `get_event_proof(chain_id, tx_hash, assertion)` | Historical proof fields, match count/indices and current `valid` |
| `is_claim_verified(chain_id, tx_hash, claim)` | Matching, unexpired adjudication consistent with current confirmed transaction evidence |
| `get_claim(chain_id, tx_hash, claim)` | `match`/`mismatch`/`indeterminate`, confidence, reasoning, metadata and current `valid` |
| `compute_topic0(signature)` | Keccak-256 event signature topic |
| `normalize_assertion(assertion)` | Validated canonical JSON string |
| `decode_logs(logs_json)` | Preview decoding of supplied log JSON; no network fetch |
| `known_event_signatures()` | Supported event signature list |
| `get_chain(chain_id)` | Registry configuration, source URLs/kinds/labels, API-key presence |
| `list_chains()` | Registered chain summaries |
| `get_config()` | Owner, TTLs, query budget, pause and submission policy |
| `stats()` | Verification/claim/proof/cache/dispute/absence counters and transaction-record count |
| `list_records(offset, limit)` | Transaction summaries, capped at 100 per read |

Transaction views expose sender/recipient, decimal-string EVM `value_wei` (null for Solana), `native_value`, `value_known`, `value_unit`, complete `accounts`/`programs`, nonce, block number, transaction index, selector, execution status, identity and settlement digests, source reports and decoded events. `sources_agreeing` counts the winning identity group; `sources_settlement_agreeing` counts the winning receipt group. `decoded_truncated` identifies summaries shortened to the 24-event storage cap.

`found` means a record exists in storage. `exists` describes external evidence. `confirmed`, `satisfied` and `tx_confirmed` describe the stored observation. `valid` and the boolean consumer predicates include current freshness/evidence checks. `epoch` versions each record independently; claim `tx_epoch` binds the adjudicated context; transaction `policy_revision`, `policy_current` and `revoked` expose currency. it is not a block number. `verified_at`/`expires_at` are deterministic Unix seconds.

## Owner administration

| Method | Parameters / behavior |
| --- | --- |
| `register_chain` | `(chain_id, name, family, evm_chain_id, min_confirmations, drift_tolerance, quorum, sources_json)`; adds/replaces a chain; family `evm` or `solana` |
| `add_source` | `(chain_id, label, kind, url, api_key)`; HTTPS required, max 8, duplicate URL rejected |
| `remove_source` | `(chain_id, label)`; removes matching labels; cannot remove every source; configured quorum remains unchanged |
| `set_chain_params` | `(chain_id, min_confirmations, drift_tolerance, quorum)`; positive confirmations/quorum, nonnegative drift |
| `set_chain_enabled` | `(chain_id, enabled)`; vetoes all cached authorization when disabled |
| `set_cache_policy` | `(ttl_final, ttl_confirming, ttl_absent)`; positive seconds, applies to new records |
| `set_max_sources` | `(max_sources)`; 1 through 8; insufficient budget fails closed for new observations |
| `set_paused` | `(paused)`; pauses submissions and vetoes positive consumer reads |
| `set_submitters_restricted` | `(restricted)`; enables allowlist, owner remains permitted |
| `set_submitter_allowed` | `(submitter: Address, allowed)` |
| `transfer_ownership` | `(new_owner: Address)` |
| `revoke_transaction` | `(chain_id, tx_hash)`; owner veto of cached authorization; fresh consensus required to replace it |
| `purge_expired` | `(limit)`; inspects at most limit records (maximum 200) across transaction, claim and proof storage; reports cursor/kind/inspection count |

A source object contains `label`, `kind`, `url`, and optional `api_key`. Kinds: `evm_jsonrpc`, `blockscout_v2`, `etherscan_v2`, `solana_jsonrpc`. Labels select removal; use unique labels. Distinct URLs do not necessarily mean distinct operators. API keys supplied on-chain are public and must not be private deployment or account credentials.

Source, threshold, cache-policy and query-budget changes increment the global policy revision and invalidate old positives. Pause and disabled chains veto positives immediately. Unpausing requires new consensus; historical facts remain readable. A refresh invalidates all earlier natural-language claims, while an event proof can remain valid only against identical supported evidence.

## Release gate

Constructor: `(verifier: Address, require_strict: bool = True)`.

| Method | Access / behavior |
| --- | --- |
| `create_condition(condition_id, chain_id, assertion, description, beneficiary: Address, deadline)` | Owner only; unique nonempty ID; canonical assertion or empty; Unix deadline 0 or future |
| `settle(condition_id, tx_hash)` | Anyone; open condition, delivered finalized snapshot matching current evidence, optional exact proof, one use per chain/transaction |
| `cancel_condition(condition_id)` | Sponsor or owner; only unsettled conditions |
| `is_settled(condition_id)` | Boolean, false for unknown conditions |
| `get_condition(condition_id)` | Full condition, parties, deadline, settlement and open/expired state |
| `list_conditions(offset, limit)` | Paginated conditions, max 100 |
| `settled_condition_for(chain_id, tx_hash)` | Consuming condition ID or empty string |
| `check_readiness(condition_id, tx_hash)` | Preview readiness/reason; does not trigger verification |
| `get_config()` | Owner, verifier, strict policy, pause, finalized-message evidence policy and condition count |
| `set_verifier(verifier: Address)` | Owner; explicit migration to a new verifier |
| `set_require_strict(require_strict)` | Owner; applies to transaction-only and event conditions |
| `receive_verification(evidence_json)` | Verifier contract only; idempotent finalized delivery |
| `set_paused(paused)` | Owner; vetoes creation and settlement |
| `transfer_ownership(new_owner: Address)` | Owner |

## Emitted events

Verifier: `TransactionVerified`, `ClaimAdjudicated`, `EventProven`, `ChainRegistered`.
Gate: `ConditionCreated`, `ConditionSettled`, `ConditionCancelled`.

Events supplement stored records; applications should account for appeals and read finalized storage before irreversible action.
