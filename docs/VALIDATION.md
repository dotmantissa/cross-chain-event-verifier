# Validation record — audit fixes, 2026-10-11

Both contracts were redeployed from source commit `570c912`. Remote byte-for-byte source checks, deployment execution results, safe gate configuration and current Ethereum verification were checked after the live run. The [manifest](../deployments/studionet.json) records addresses, hashes, actual parent/child transaction IDs and execution evidence. [Audit findings and remedies](AUDIT.md) describe what changed.

| Check | Result |
| --- | --- |
| Both contracts: GenVM lint and typecheck | Passed locally and in CI |
| Direct verifier/adversarial/transport cases | 576 passed |
| Separate direct release-gate cases | 16 passed |
| Live verifier cases | 24 passed |
| Distinct live release-gate cases | 10 passed across identical-source deployments |
| Positive consumer authorization | Actual finalized-message delivery, followed by current-state veto check |
| Ethereum event settlement and replay | New settlement succeeded; same-condition and second-condition replay refused |
| Published remote source | All three deployment sources match local files byte for byte |
| Governance restoration | Strict policy enabled, gates unpaused, temporary test chains disabled |

[CI passed on the exact contract source commit](https://github.com/dotmantissa/cross-chain-event-verifier/actions/runs/38131490592). The subsequent integration-test commit adds live access-control, pause, explicit revocation and canonical-proof restoration checks without changing either deployed contract.

The full live run had 33 passing cases and one RPC transport error while polling an already submitted publication. Read-only recovery established that this parent and its actual emitted child both finalized successfully. No unresolved write was resubmitted. The remaining event settlement/replay case then passed in a focused run against a fresh, identical-source gate. This is 34 distinct passing cases across runs, rather than a claim that one uninterrupted full run passed.

`CrossChainReleaseGate` in the manifest is the fresh deployment used for the complete event lifecycle. `CrossChainReleaseGateLifecycleValidation` is the identical-source deployment used for the other nine gate cases. Positive authorization requires the authenticated finalized callback on both; neither has an unsafe non-final positive mode. The historic diagnostic consumer and failed finalized-storage-read deployment are retained only in the [superseded manifest](../deployments/studionet-2026-10-10.superseded.json).

Live verifier checks cover a historical Ethereum USDT transfer's exact sender, recipient, block, transaction index, nonce, selector and amount; corroborated identity/settlement evidence; confirmation policy; correct and incorrect event assertions; quorum-based absence; invalid inputs; canonicalization/decoding views; and matching/mismatching natural-language claims under independent validator execution.

Live gate checks follow actual publication and emitted-child IDs to successful finalization, refresh by polling its stored transaction ID, read updated chain state, veto old delivery, republish, revoke and republish again, then settle. They also cover strict versus permissive confirmation policy, an actual outsider-account registration refusal, pause, cancellation, missing conditions and unverified transactions. Temporary registry tests invalidate prior generations; canonical Ethereum proof is refreshed and delivered under the restored policy afterward.

Direct tests exercise conflicting validator decisions, all consumed fact commitments, Solana log-boundary/case collisions, malformed-source fallback, v0 loaded addresses, unknown native values, cache/policy/epoch invalidation, bounded cleanup, transport and rollback-sensitive gate logic. These controlled tests do not demonstrate a live contested appeal or every remote-chain reorganization. The finalized-message tests demonstrate successful scheduling after the source finalization window, not an adversarial appeal being submitted and reversed.

Only Ethereum has live transaction evidence here. Other registered chains remain experimental configurations with direct adapter coverage. External source quorum is not a cryptographic inclusion proof; depth-based finality is application policy. The reference consumer records authorization, holds no funds and scopes consumed-transaction history to its deployment. Mission-specific acceptance cannot be guaranteed by these checks.
