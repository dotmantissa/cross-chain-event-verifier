# Audit findings and implemented remedies

The audit examined contracts, adapters, consensus validation, storage/cache currency, the reference consumer, deployment tooling, tests and published claims at `58439d3`. Controlled probes reproduced the defects before fixes. Contract fixes are in source commit `570c912`; [validation](VALIDATION.md) and the [deployment manifest](../deployments/studionet.json) record evidence for that exact source.

| Finding | Implemented remedy | Verification |
| --- | --- | --- |
| Validators could agree across a confirmation threshold while persisting different authorization decisions | Exact agreement on authorization, finality class, mined state and all consumed facts; bounded depth drift cannot change decisions | Saved validator callbacks and threshold regressions |
| Solana accounts/programs and some claim inputs were absent from commitments | Complete supported identity and settlement facts; claims bind rendered evidence and transaction epoch | Source quorum, conflicting accounts/programs and refreshed-claim regressions |
| Log commitment delimiters and case folding could alias different Solana evidence | Canonical JSON vector encoding preserves boundaries and Solana case | Both collision probes failed before the fix and pass afterward |
| Malformed mandatory fields could become fabricated defaults; one malformed source could disable healthy fallback | Strict required schemas and per-source error/cap isolation | Malformed EVM/explorer/Solana and healthy-quorum tests |
| Solana v0 loaded addresses were omitted and unknown transfer values appeared as zero | Resolve loaded addresses and invoked programs; explicitly unknown amount and correct value unit | v0 normalization tests and unsupported-value assertions |
| Outsiders could create conditions and consume intended authorization or occupy IDs | Owner-only condition registration; verifier-only evidence receipt; replay protection retained | Separate direct gate suite and a real outsider account rejection |
| Default finalized storage-read consumer had no successful hosted demonstration | Positive evidence delivered by authenticated `emit(on="finalized")`; exact current-state read can veto that snapshot | Actual parent/child finalization, readiness, settlement and replay lifecycle |
| Refresh, policy changes, pause and revocation could leave old positives usable | Policy revisions, monotonic transaction epochs, explicit revocation and gate pause | Cache regressions and live refresh/revoke/republication/pause tests |
| Cleanup could scan all state and leak claim/proof records | Bounded round-robin transaction/claim/proof indexes and bounded inspected entries | Cleanup and epoch-preservation tests |
| RPC waits could hang and interrupted deployments could be blindly duplicated | Bounded transport, read-only retries, journaled IDs and explicit failed-deployment recovery | Transport tests and live recovery of a timed-out publication without resubmitting it |
| Consumer coverage was missing in CI and reused settlement could mask a lifecycle gap | Separate direct gate CI job; fresh-gate integration requires a new settlement | 16 direct gate cases and 10 distinct live gate cases |

The implementation addresses these reproduced findings. It does not establish universal submission acceptance, cryptographic source truth, production performance at every state size or remote protocol finality. The primitive is an owner-governed consensus oracle. Only Ethereum has published live transaction evidence; the other configured chains remain experimental. Solana program-log checks do not prove SOL/SPL transfers, and unsupported transfer amounts remain unknown. The reference gate records authorization and holds no funds. Source upgrades or new gates require deliberate preservation of replay history in downstream applications.

A review of the original submission would have read:

> The contract cannot be accepted in its original form because validators can accept different authorization decisions, and source commitments omit facts used in Solana and claim adjudication. Malformed evidence can become defaults or prevent healthy-source quorum. Permissionless condition registration permits outsiders to consume an intended authorization. Cached positives remain usable after policy changes, and the default finalized consumer has no successful deployed lifecycle evidence. Bind every consumed fact and decision, enforce current evidence and access control, bound cleanup and transport, and submit matching deployed source with a complete finalized delivery, settlement and replay test.

That note describes the audited pre-fix source. Current validation and remaining trust boundaries are documented separately.
