# Testing, deployment and recovery

## Local checks

Install `requirements-dev.txt` into a virtual environment. Both contracts pin:

```text
py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6
```

Run lint and typecheck before direct tests:

```bash
genvm-lint check contracts/cross_chain_event_verifier.py
genvm-lint typecheck contracts/cross_chain_event_verifier.py
genvm-lint check contracts/examples/cross_chain_release_gate.py
genvm-lint typecheck contracts/examples/cross_chain_release_gate.py
pytest -p no:gltest tests/direct -q
```

The direct-test command disables the network configuration plugin (`gltest`) while retaining `gltest_direct`. This avoids loading live account settings or clearing integration artifacts. CI runs these checks without account credentials. Direct tests cover normalization, decoding, assertions, digests, aggregation, saved validator callbacks, registry governance, access control, cache expiry and reorg invalidation. They do not replace real GenVM execution. The SDK allows one contract class in a direct-mode process; the release gate's real cross-contract behavior is tested on a network.

## Live validation

```bash
cp .env.example .env
# Populate CCEV_DEPLOYER_KEY privately.
gltest tests/integration -v -s --network studionet -x
```

This suite uses real sources, model calls, consensus and finalized cross-contract reads. It checks the historical Ethereum USDT transaction against fixed sender/recipient/amount/block/nonce/index fixtures, wrong event assertions, explicit absence, malformed inputs, claim match/mismatch, replay protection, cancellation and strict event policy.

Session-scoped deployments keep shared-host request costs down. `deployments/studionet.pending.json` stores transaction IDs before waiting, plus source hashes, constructor arguments and finalized addresses after successful execution. A restart reuses only a matching deployment. `CCEV_DEPLOYMENTS_FILE` chooses another checkpoint. Never promote a checkpoint as a validated deployment until contract execution and intended live behavior pass.

StudioNet reads use `scripts/transport.py` for raw calldata. During validation on 2026-10-10, the installed SDK's valid RLP envelope caused hosted `gen_call` to fail with `RLP string ends with ... superfluous bytes` for long JSON arguments; the same payload as raw calldata succeeded. The adapter only changes reads to `studio.genlayer.com`. SDK transaction signing, consensus and writes remain unchanged. Unit tests cover long arguments, finalized-state selection, sender override and endpoint isolation; live tests exercise the actual hosted path.

Polling uses 10-second intervals with bounded retries. Throttle heavy live runs; a failed external request or hosted limit is not evidence that the external transaction is absent. Hosted finalization can take minutes. Preserve the transaction ID and inspect it rather than blindly resubmitting.

## Deploy or interact independently

The typed helper avoids the installed CLI's auto-coercion of numeric-looking chain IDs, hashes and JSON assertion strings:

```bash
python3 scripts/ccev.py deploy contracts/cross_chain_event_verifier.py \
  --args-json '[true]' --checkpoint deployments/my-verifier.pending.json
python3 scripts/ccev.py deploy contracts/examples/cross_chain_release_gate.py \
  --args-json '[{"$address":"0xYOUR_VERIFIER_ADDRESS"},true]' \
  --checkpoint deployments/my-gate.pending.json
python3 scripts/ccev.py call 0xYOUR_VERIFIER_ADDRESS get_config
python3 scripts/ccev.py receipt 0xYOUR_TRANSACTION_HASH
```

Deployment checkpoints are reused only for matching network, source and arguments. Call commands read finalized storage by default (`--state non-final` is explicit). Write/deploy commands wait for successful finalized execution. A `Transaction:` line is printed before waiting; save it to recover an interrupted write. Keys are loaded from `.env` or the environment and never from command-line options. For ABI `Address` parameters, use an explicit `{"$address":"0x..."}` argument tag; ordinary JSON strings remain strings.

`genlayer network set studionet`, `genlayer call ADDRESS get_config`, and `genlayer receipt HASH --stdout --stderr` remain useful for CLI diagnostics. `ACCEPTED` or `FINALIZED` alone does not mean contract execution succeeded. Inspect the execution result; failed execution applies no state changes.

## Upgrades and governance

Contracts are immutable: redeploy changed source, run live validation, then repoint consumers with `set_verifier`. The gate's owner can perform this migration, but a new gate does not inherit the previous deployment's consumed-transaction history. Preserve replay protection deliberately in applications migrating authorizations.

Update source operators and confirmation policy based on each chain's settlement assumptions. Set a query budget that can reach quorum. Removing a source never lowers quorum automatically. Existing cache policy/evidence stays in effect until refresh or expiry; changing TTLs affects newly written records.

The local `TASKS.md` recovery checklist is excluded through Git metadata and is never committed. It records unfinished work, operation IDs and verification evidence without credentials. Published manifests contain only addresses, transaction IDs, source hashes, public test fixtures and validation summaries. `.env`, pending checkpoints and runtime artifacts are ignored.
