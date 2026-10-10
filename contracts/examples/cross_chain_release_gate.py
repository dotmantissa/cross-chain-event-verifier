# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Cross-Chain Release Gate
========================

A reference consumer of the Cross-Chain Event Verifier, and a usable primitive
in its own right.

A sponsor registers a condition: "on chain X, a transaction must emit this
event". Anyone may then settle that condition by pointing at a transaction
hash. The gate asks the verifier whether that transaction was proven, and if it
was, records the condition as met. Downstream contracts and dApps read
``is_settled`` and act on it.

Why this contract has no equivalence principle
-----------------------------------------------
It performs no non-deterministic work at all. There are no web requests and no
language model calls, so there is nothing for validators to disagree about. All
of the subjective work already happened inside the verifier, under consensus,
and was cached. This gate only reads that cached result.

That is the point of the verifier being a separate primitive: consensus is paid
for once, by one contract, and every consumer afterwards is plain deterministic
logic.

The two-step settlement flow
-----------------------------
Cross-contract *writes* on GenLayer are asynchronous (``emit()`` queues a call
for after the current transaction), while cross-contract *reads* are
synchronous. A gate cannot therefore trigger a verification and read its result
in one transaction. Settlement is two steps:

1. Someone calls ``verify_transaction`` or ``prove_event`` on the verifier.
   Consensus runs and the result is cached on-chain.
2. Someone calls ``settle`` here. The gate reads the cached result
   synchronously and decides.

Step 1 is permissionless on the verifier, so anyone can perform it, including
the same actor who calls step 2. The verifier's cache is what makes step 2
cheap and deterministic.

Reading against finalised state
--------------------------------
``gl.get_contract_at(addr).view(state=...)`` chooses which version of the
verifier's storage to read. A gate that releases value should read
``LATEST_FINAL``, so it cannot act on a verifier record that is still
rollback-eligible. That is the default here, and ``allow_non_final`` exists for
callers who would rather have a faster answer than a final one.
"""

import datetime
import json
from dataclasses import dataclass

from genlayer import *
from genlayer.py.public_abi import StorageType


ERROR_EXPECTED = "[EXPECTED]"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@allow_storage
@dataclass
class Condition:
    condition_id: str
    chain_id: str
    assertion: str
    """Canonical event assertion, or empty for a transaction-existence check."""
    description: str
    sponsor: Address
    beneficiary: Address
    deadline: u256
    """Unix seconds. 0 means no deadline."""
    settled: bool
    settled_by: Address
    settled_tx: str
    settled_at: u256
    cancelled: bool
    created_at: u256


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class ConditionCreated(gl.Event):
    def __init__(self, condition_id: str, /, **blob): ...


class ConditionSettled(gl.Event):
    def __init__(self, condition_id: str, /, **blob): ...


class ConditionCancelled(gl.Event):
    def __init__(self, condition_id: str, /, **blob): ...


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class CrossChainReleaseGate(gl.Contract):
    """Releases an authorisation once a cross-chain event has been verified."""

    owner: Address
    verifier: Address
    require_strict: bool
    """Demand is_verified_strict, rejecting borderline confirmation depth."""
    allow_non_final: bool
    """Read verifier storage that is not yet finalised. Off by default."""

    condition_ids: DynArray[str]
    conditions: TreeMap[str, Condition]
    consumed_tx: TreeMap[str, str]
    """chain|tx_hash -> condition_id. Blocks reusing one transaction twice."""

    def __init__(self, verifier: Address, require_strict: bool = True) -> None:
        self.owner = gl.message.sender_address
        self.verifier = verifier
        self.require_strict = require_strict
        self.allow_non_final = False

    # -- internals ---------------------------------------------------------

    def _now(self) -> int:
        raw = str(gl.message_raw["datetime"]).strip()
        text = raw.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unparseable datetime {raw}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return int(parsed.timestamp())

    def _require_owner(self) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} owner only")

    def _verifier_view(self):
        """A read namespace on the verifier.

        Finalised state by default, so a release decision cannot be made
        against a verifier record that is still rollback-eligible.
        """
        state = (
            StorageType.LATEST_NON_FINAL if self.allow_non_final else StorageType.LATEST_FINAL
        )
        return gl.get_contract_at(self.verifier).view(state=state)

    def _condition(self, condition_id: str) -> Condition:
        key = str(condition_id).strip()
        if key not in self.conditions:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown condition {key}")
        return self.conditions[key]

    def _view(self, condition: Condition, now: int) -> dict:
        expired = int(condition.deadline) > 0 and now >= int(condition.deadline)
        return {
            "condition_id": condition.condition_id,
            "chain_id": condition.chain_id,
            "assertion": condition.assertion,
            "description": condition.description,
            "sponsor": condition.sponsor.as_hex,
            "beneficiary": condition.beneficiary.as_hex,
            "deadline": int(condition.deadline),
            "settled": bool(condition.settled),
            "settled_by": condition.settled_by.as_hex,
            "settled_tx": condition.settled_tx,
            "settled_at": int(condition.settled_at),
            "cancelled": bool(condition.cancelled),
            "created_at": int(condition.created_at),
            "expired": expired and not bool(condition.settled),
            "open": not bool(condition.settled)
            and not bool(condition.cancelled)
            and not expired,
        }

    # -- sponsor surface ---------------------------------------------------

    @gl.public.write
    def create_condition(
        self,
        condition_id: str,
        chain_id: str,
        assertion: str,
        description: str,
        beneficiary: Address,
        deadline: int,
    ) -> dict:
        """Register a cross-chain condition.

        ``assertion`` is a JSON event assertion in the verifier's format, or an
        empty string to require only that the transaction exists and is
        confirmed. Passing it through the verifier's ``normalize_assertion``
        first means the stored form matches the key the verifier will look up.

        ``deadline`` is unix seconds, or 0 for no deadline.
        """
        key = str(condition_id).strip()
        if not key:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} condition_id must not be empty")
        if key in self.conditions:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} condition {key} already exists")
        chain = str(chain_id).strip()
        if not chain:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} chain_id must not be empty")

        canonical = ""
        spec = str(assertion).strip()
        if spec:
            # Canonicalise through the verifier so the stored assertion is
            # exactly the key the verifier will be asked about later. Doing it
            # here rather than at settlement time means a malformed assertion
            # is rejected when it is cheap to fix.
            canonical = str(self._verifier_view().normalize_assertion(spec))

        now = self._now()
        if int(deadline) < 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} deadline must not be negative")
        if int(deadline) > 0 and int(deadline) <= now:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} deadline is already past")

        condition = Condition(
            condition_id=key,
            chain_id=chain,
            assertion=canonical,
            description=str(description),
            sponsor=gl.message.sender_address,
            beneficiary=beneficiary,
            deadline=u256(int(deadline)),
            settled=False,
            settled_by=gl.message.sender_address,
            settled_tx="",
            settled_at=u256(0),
            cancelled=False,
            created_at=u256(now),
        )
        self.conditions[key] = condition
        self.condition_ids.append(key)

        ConditionCreated(
            key,
            chain_id=chain,
            beneficiary=beneficiary.as_hex,
            deadline=int(deadline),
            has_assertion=bool(canonical),
        ).emit()
        return self._view(self.conditions[key], now)

    @gl.public.write
    def cancel_condition(self, condition_id: str) -> dict:
        """Withdraw an unsettled condition. Sponsor or owner only."""
        condition = self._condition(condition_id)
        sender = gl.message.sender_address
        if sender != condition.sponsor and sender != self.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} sponsor or owner only")
        if condition.settled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} condition is already settled")
        if condition.cancelled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} condition is already cancelled")

        condition.cancelled = True
        ConditionCancelled(str(condition_id).strip(), by=sender.as_hex).emit()
        return self._view(condition, self._now())

    # -- settlement --------------------------------------------------------

    @gl.public.write
    def settle(self, condition_id: str, tx_hash: str) -> dict:
        """Settle a condition against a transaction the verifier has checked.

        Reads the verifier synchronously. Requires that the verifier has
        already run consensus on this transaction: call ``prove_event`` (or
        ``verify_transaction`` for an assertion-free condition) on the verifier
        first. Nothing here is non-deterministic, so this transaction needs no
        equivalence principle of its own.
        """
        condition = self._condition(condition_id)
        now = self._now()

        if condition.settled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} condition is already settled")
        if condition.cancelled:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} condition is cancelled")
        if int(condition.deadline) > 0 and now >= int(condition.deadline):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} condition deadline has passed")

        tx = str(tx_hash).strip()
        if not tx:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} tx_hash must not be empty")

        # Replay protection. One cross-chain transaction settles at most one
        # condition, so a single deposit cannot be used to claim twice.
        consumed_key = f"{condition.chain_id}|{tx.lower()}"
        if consumed_key in self.consumed_tx:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} transaction already settled condition "
                f"{self.consumed_tx[consumed_key]}"
            )

        reader = self._verifier_view()

        if condition.assertion:
            proven = bool(
                reader.is_event_proven(condition.chain_id, tx, condition.assertion)
            )
            if not proven:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} verifier has not proven this event for {tx}"
                )
        else:
            if self.require_strict:
                confirmed = bool(reader.is_verified_strict(condition.chain_id, tx))
            else:
                confirmed = bool(reader.is_verified(condition.chain_id, tx))
            if not confirmed:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} verifier has not confirmed transaction {tx}"
                )

        condition.settled = True
        condition.settled_by = gl.message.sender_address
        condition.settled_tx = tx
        condition.settled_at = u256(now)
        self.consumed_tx[consumed_key] = condition.condition_id

        ConditionSettled(
            condition.condition_id,
            chain_id=condition.chain_id,
            tx_hash=tx,
            beneficiary=condition.beneficiary.as_hex,
            settled_by=gl.message.sender_address.as_hex,
        ).emit()
        return self._view(condition, now)

    # -- reads -------------------------------------------------------------

    @gl.public.view
    def is_settled(self, condition_id: str) -> bool:
        """The call downstream contracts and dApps rely on."""
        key = str(condition_id).strip()
        if key not in self.conditions:
            return False
        return bool(self.conditions[key].settled)

    @gl.public.view
    def get_condition(self, condition_id: str) -> dict:
        key = str(condition_id).strip()
        if key not in self.conditions:
            return {"condition_id": key, "found": False, "settled": False}
        out = self._view(self.conditions[key], self._now())
        out["found"] = True
        return out

    @gl.public.view
    def list_conditions(self, offset: int, limit: int) -> dict:
        start = max(0, int(offset))
        count = max(0, min(int(limit), 100))
        total = len(self.condition_ids)
        now = self._now()
        out = []
        for index in range(start, min(start + count, total)):
            out.append(self._view(self.conditions[self.condition_ids[index]], now))
        return {"conditions": out, "total": total, "offset": start, "returned": len(out)}

    @gl.public.view
    def settled_condition_for(self, chain_id: str, tx_hash: str) -> str:
        """Which condition a given transaction already settled, if any."""
        key = f"{str(chain_id).strip()}|{str(tx_hash).strip().lower()}"
        return self.consumed_tx.get(key, "")

    @gl.public.view
    def check_readiness(self, condition_id: str, tx_hash: str) -> dict:
        """Dry run a settlement without sending a transaction.

        Lets a dApp show the user why settlement would fail, and in particular
        whether the verifier still needs to be asked about the transaction.
        """
        key = str(condition_id).strip()
        if key not in self.conditions:
            return {"ready": False, "reason": "unknown condition"}

        condition = self.conditions[key]
        now = self._now()
        tx = str(tx_hash).strip()

        if condition.settled:
            return {"ready": False, "reason": "already settled"}
        if condition.cancelled:
            return {"ready": False, "reason": "cancelled"}
        if int(condition.deadline) > 0 and now >= int(condition.deadline):
            return {"ready": False, "reason": "deadline passed"}
        if f"{condition.chain_id}|{tx.lower()}" in self.consumed_tx:
            return {"ready": False, "reason": "transaction already used"}

        reader = self._verifier_view()
        record = reader.get_verification(condition.chain_id, tx)
        if not bool(record.get("found")):
            return {
                "ready": False,
                "reason": "verifier has no record; run a verification first",
                "verifier_status": "unverified",
            }

        if condition.assertion:
            proven = bool(
                reader.is_event_proven(condition.chain_id, tx, condition.assertion)
            )
            return {
                "ready": proven,
                "reason": "" if proven else "event not proven",
                "verifier_status": str(record.get("status", "")),
            }

        if self.require_strict:
            confirmed = bool(reader.is_verified_strict(condition.chain_id, tx))
        else:
            confirmed = bool(reader.is_verified(condition.chain_id, tx))
        return {
            "ready": confirmed,
            "reason": "" if confirmed else "transaction not confirmed",
            "verifier_status": str(record.get("status", "")),
        }

    @gl.public.view
    def get_config(self) -> dict:
        return {
            "owner": self.owner.as_hex,
            "verifier": self.verifier.as_hex,
            "require_strict": bool(self.require_strict),
            "allow_non_final": bool(self.allow_non_final),
            "condition_count": len(self.condition_ids),
        }

    # -- admin -------------------------------------------------------------

    @gl.public.write
    def set_verifier(self, verifier: Address) -> dict:
        """Repoint at a different verifier deployment, for upgrades."""
        self._require_owner()
        self.verifier = verifier
        return self.get_config()

    @gl.public.write
    def set_require_strict(self, require_strict: bool) -> dict:
        self._require_owner()
        self.require_strict = bool(require_strict)
        return self.get_config()

    @gl.public.write
    def set_allow_non_final(self, allow_non_final: bool) -> dict:
        """Permit reads against non-finalised verifier state.

        Faster, but a settlement decided this way can be made against a
        verifier record that is later rolled back. Leave off when settlement
        releases value.
        """
        self._require_owner()
        self.allow_non_final = bool(allow_non_final)
        return self.get_config()

    @gl.public.write
    def transfer_ownership(self, new_owner: Address) -> dict:
        self._require_owner()
        self.owner = new_owner
        return self.get_config()
