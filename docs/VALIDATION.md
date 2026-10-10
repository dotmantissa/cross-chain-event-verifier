# Validation record — 2026-10-10

The verifier's deployed source matches the local contract byte for byte. All 24 live verifier cases passed on that source, using real Ethereum archive RPC/explorer requests, independent validator execution and hosted LLM adjudication. The release gate's consumer behavior passed six live cases on a separate deployment explicitly configured for non-final cross-contract reads. Default finalized cross-contract reads remain blocked by a hosted VM timeout.

| Check | Result |
| --- | --- |
| Both contracts: GenVM lint and typecheck | Passed locally and in CI |
| Direct contract, adversarial and transport tests | 544 passed |
| Live verifier cases | 24 passed across filtered runs on identical source |
| Diagnostic gate cases | 6 passed across filtered runs with `allow_non_final=True` |
| Default gate configuration | Strict policy enabled, finalized reads selected |
| Default gate cross-contract read | Finalized with execution ERROR / VM timeout |
| Published contracts' remote source hashes | Match local source |

[Deployment addresses, source hashes and finalized execution evidence](../deployments/studionet.json) are recorded separately from ignored recovery checkpoints. [CI](https://github.com/dotmantissa/cross-chain-event-verifier/actions/runs/38089204623) passed on implementation/tooling commit `440fd60`; the publication commit adds documentation and deployment evidence.

The live verifier cases check the historical Ethereum transaction's sender, recipient, block, transaction index, nonce, selector and ERC20 log amount; multiple identity and settlement sources; confirmation depth; exact event proof, wrong amount and wrong recipient; quorum-based absence and expiry; invalid inputs; canonicalization/decoding views; and matching/mismatching natural-language claims. The direct suite covers the other adapters and adversarial equivalence/cache/transport cases. No live transaction validation is claimed for the other registered chains.

The diagnostic consumer cases check successful settlement, case-normalized EVM replay prevention, unverified transaction refusal, cancellation, safe missing-condition reads and strict confirmation policy. The strict-policy regression creates a real event proof inside a deliberately wide confirmation band: `is_event_proven` is true while `is_verified_strict` is false. The strict gate refuses it; an explicit owner policy change permits settlement, after which strict policy is restored and the temporary chain is disabled.

The default gate's failed call applied no condition state changes. The Studio source rejects finalized storage selection in its host handler; this is consistent with the timeout, but the deployed backend revision is not exposed. Successful non-final diagnostics do not establish finalized cross-contract safety. See [operations and the pinned upstream source reference](OPERATIONS.md#hosted-finalized-cross-contract-read-limitation). Validate the default policy on a supporting host before value-sensitive use. The alternative Bradbury account had zero balance and its intelligent-contract RPC was unreachable from this environment, so no Bradbury deployment was attempted.
