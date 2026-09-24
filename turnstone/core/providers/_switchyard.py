"""Switchyard provider adapter — the execution boundary for Switchyard's lanes.

Responsibility split (maintainer direction, 2026-09-21 — ``Turn IR -> lowering
-> provider``): Turnstone owns ledger/session truth, resumed-turn and tool-round
semantics, reasoning/replay state, whether reasoning material exists at all,
whether that material is transferable to another provider, the information-loss
classification and the provider-neutral execution requirements.  Switchyard owns
endpoint/model selection, fleet readiness, local/cloud topology, cost,
escalation, thinking controls on the selected endpoint and GPU/model
availability.

The adapter therefore decides nothing about routing.  It lowers the neutral
ledger into the request Switchyard's surface accepts and states what had to be
left behind when reasoning material cannot cross the provider boundary.

Cross-provider reasoning rule
-----------------------------
Encrypted or native-signed reasoning is not interchangeable with plain
``reasoning_text``: such an item is bound server-side, either to an id only the
issuing endpoint can resolve or to a signature over the exact prefix that
produced it.  Handing the binding to a different provider either fails or
silently changes the model's context, so this adapter does not forward it.  It
removes the binding and reports the crossing the way the surface's wire actually
carries it, so the caller can record what the boundary cost.  Nothing is
substituted for what was removed, and a block that held no readable text does not
cross at all.

What removing a binding costs depends on the surface, because the two surfaces do
not represent reasoning the same way.  A Responses surface carries the reasoning
item itself, and the parent's own projection cannot build one without a string
``id`` (``_openai_responses._reasoning_item_for_input`` returns ``None`` and the
caller skips appending), so on that surface a removed binding does not leave a
text-only item behind: it removes the item, and the block is reported as dropped
rather than as a strip.  A Chat surface has no reasoning-item representation at
all -- its wire carries the readable text through the canonical message field and
sanitization drops the private block list -- so a binding it could never carry is
not a cost, and the block is reported as kept.  ``_SURFACE_CARRIERS`` states this
per surface; the count is only ever ``kept`` or ``stripped`` when that surface's
own projection will emit the material.

Round-repair (``synthesizes_round_reasoning``) is proposed in an unmerged upstream
change (#1201) and is not present in this tree: no code, capability flag or
default here implements it.  Replay support and repair authorisation are separate
questions, and a commercial Responses endpoint rejects a fabricated round item.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from turnstone.core.providers._openai_chat import OpenAIChatCompletionsProvider
from turnstone.core.providers._openai_responses import OpenAIResponsesProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from turnstone.core.providers._protocol import StreamChunk

PROVIDER_NAME = "switchyard"

# ─── Information-loss classification ────────────────────────────────────────
# Verified against Switchyard's own translation codecs (RedEyeNinja-BKK/Switchyard
# @ 7a23989): a Responses reasoning item is decoded for its text — ``content``,
# ``summary`` and a top-level ``text`` (see
# ``crates/switchyard-translation/src/codecs/responses/buffered.rs:649``) — and the
# binding it arrived with is not carried anywhere downstream
# (``ContentBlock::Reasoning { signature: None, details: [] }``); ``grep -rn
# encrypted_content crates/`` has no hits at all.  Text is therefore the part that
# transfers and the binding is the part that is lost, so this adapter keeps the
# text, removes the binding and reports which binding went missing, instead of
# letting the loss happen unannounced.

LOSS_FOREIGN_ENCRYPTED = "foreign_encrypted_binding_dropped"
LOSS_NATIVE_SIGNED = "native_signed_binding_dropped"
LOSS_UNREPRESENTABLE = "reasoning_content_unrepresentable"
# The readable text survived the lowering, but the surface's own projection reads a
# handle that the lowering removed, so there is no item left to emit: the text does
# not cross either, and reporting it as a strip would name a cost that is not the
# one the wire paid.
LOSS_ITEM_UNREPRESENTABLE = "reasoning_item_unrepresentable_without_binding"
# The calling parent projects native blocks only for the producer that recorded them
# (``_openai_responses._convert_messages`` keeps a block whose ``_producer`` is absent
# or equal to the surface's ``native_producer``), so a block recorded on another lane
# is never read, whatever its shape.
LOSS_FOREIGN_PRODUCER = "reasoning_item_ignored_for_foreign_producer"

# Reasoning block shapes whose text this adapter knows how to lower.
_REASONING_BLOCK_TYPES = ("reasoning", "reasoning_text", "thinking")
# The two ways a block stops being plain text, checked in this order: a signed
# item and a foreign-encrypted item.
_SIGNATURE_FIELDS = ("signature", "thinking_signature")
_ENCRYPTED_FIELDS = ("encrypted_content", "encrypted_reasoning")
# Everything removed from a bound block before its text crosses.  The item id sits
# here with the blobs: it is the handle that resolves a stored item at the endpoint
# that issued it, so a different endpoint cannot dereference it either.
_BINDING_FIELDS = (*_SIGNATURE_FIELDS, *_ENCRYPTED_FIELDS, "id")


@dataclass(frozen=True)
class _SurfaceCarrier:
    """What a surface's wire needs for reasoning material to be representable on it.

    ``required_bindings`` are the string handles that surface's projection reads to
    build an item.  ``native_producer`` is the producer name whose native blocks that
    surface's parent will project, or ``None`` where the parent does not select native
    blocks by producer at all.  ``binding_is_carried`` is False where the wire has no
    field for binding metadata whatsoever, so removing one costs nothing that surface
    would otherwise have carried.
    """

    required_bindings: tuple[str, ...]
    native_producer: str | None
    binding_is_carried: bool


_SURFACE_CARRIERS: dict[str, _SurfaceCarrier] = {
    # Responses carries the reasoning item, and the item needs its handle.  It also
    # round-trips binding metadata itself, which is why a removal an item survived
    # would be a strip rather than a change that costs nothing.
    "responses": _SurfaceCarrier(
        required_bindings=("id",), native_producer=PROVIDER_NAME, binding_is_carried=True
    ),
    # Chat carries the readable text in the canonical message field and never
    # projects the private block list, so it needs no binding and can lose none.
    "chat": _SurfaceCarrier(required_bindings=(), native_producer=None, binding_is_carried=False),
}


def _is_representable(block: dict[str, Any], required_bindings: tuple[str, ...]) -> bool:
    """Whether this surface's projection can still build an item from *block*.

    Mirrors the parent's own requirement: it reads a string handle per required
    binding and yields nothing when one is missing, empty, or of the wrong type, so a
    non-string value is not representable either.
    """
    for field in required_bindings:
        value = block.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


def _plaintext_of(block: dict[str, Any]) -> str:
    """Return the plain reasoning text carried by *block*, or ``""``.

    Reads the same fields Switchyard's own decoder reads, so what this adapter
    considers transferable is what the receiving codec will actually pick up.
    """
    for key in ("reasoning_text", "text", "thinking"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    parts: list[str] = []
    for key in ("summary", "content"):
        for part in block.get(key) or []:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts)


def lower_reasoning_block(block: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return what may cross to another provider's endpoint, and what that costs.

    ``(block, None)`` when the block crosses unchanged, ``(lowered, loss)`` when it
    crosses with its binding removed, and ``(None, loss)`` when nothing readable is
    left.  ``(None, None)`` means the block held no reasoning text: nothing to carry
    is not the same as something lost, so it is neither kept nor reported.

    Bindings are checked signature-first, so a block bound both ways is reported
    once under the signature class; every binding field is removed either way.
    """
    text = _plaintext_of(block)
    signed = any(block.get(field) for field in _SIGNATURE_FIELDS)
    encrypted = any(block.get(field) for field in _ENCRYPTED_FIELDS)
    if signed or encrypted:
        if not text:
            # The binding is the reasoning: no endpoint but the issuer's can resolve
            # the blob or check the signature, so there is nothing readable to carry.
            return None, LOSS_UNREPRESENTABLE
        lowered = {key: value for key, value in block.items() if key not in _BINDING_FIELDS}
        return lowered, LOSS_NATIVE_SIGNED if signed else LOSS_FOREIGN_ENCRYPTED
    if not text:
        return None, None
    return block, None


@dataclass
class ReasoningTransfer:
    """Outcome of one boundary crossing: what crossed and what it cost."""

    kept: int = 0
    """Blocks whose readable reasoning material this surface's wire carries."""
    stripped: int = 0
    """Blocks that still cross with binding metadata removed.

    Meaningful only for a surface whose wire both carries bindings and tolerates a
    missing one, which no surface declares today: every binding this adapter removes
    is one the surface either needs to build the item (Responses, where the item is
    then dropped) or cannot carry at all (Chat, where the text still crosses and the
    removal costs nothing), so a removal lands in ``dropped`` or in ``kept``.
    """
    dropped: int = 0
    """Blocks whose reasoning material this surface's wire does not receive."""
    losses: tuple[str, ...] = ()
    """Distinct loss classes, in the order they were first seen."""

    @property
    def lossy(self) -> bool:
        return bool(self.losses)


def retain_transferable_reasoning(
    messages: list[dict[str, Any]],
    *,
    required_bindings: tuple[str, ...],
    native_producer: str | None,
    binding_is_carried: bool,
) -> tuple[list[dict[str, Any]], ReasoningTransfer]:
    """Rebuild *messages* with only what this boundary can carry.

    Returns ``(messages, transfer)``.  The list and any message it changes are new
    objects: a caller's ledger objects stay untouched, including the blocks that were
    lowered.  Only ``_provider_content`` is inspected, and only blocks whose type is a
    recognised reasoning shape; canonical ``content``, ``tool_calls`` and any block
    this adapter does not interpret pass through unchanged, so the receiving provider
    is never handed a rewritten tool history.

    *required_bindings*, *native_producer* and *binding_is_carried* are the surface
    being lowered for (see ``_SURFACE_CARRIERS``) and they decide what counts as
    crossed: a block is only ``kept`` or ``stripped`` when that surface's own
    projection will emit it.  A block that lost a handle the surface's projection
    reads is a drop, because the readable text does not reach the wire without it; a
    block whose removed binding is one the wire has no field for stays a crossing,
    since nothing it would have carried was taken; and a message whose native blocks
    the calling parent will not project at all -- recorded by a producer other than
    the surface's own -- is a drop whatever the block's shape was, since that shape is
    not what decided the outcome.
    """
    out: list[dict[str, Any]] = []
    kept = stripped = dropped = 0
    losses: list[str] = []

    def _note(loss: str | None) -> None:
        if loss and loss not in losses:
            losses.append(loss)

    for message in messages:
        blocks = message.get("_provider_content")
        if not isinstance(blocks, list) or not blocks:
            out.append(message)
            continue
        producer = message.get("_producer")
        foreign_producer = bool(
            native_producer is not None and producer and producer != native_producer
        )
        surviving: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in _REASONING_BLOCK_TYPES:
                surviving.append(block)
                continue
            lowered, loss = lower_reasoning_block(block)
            if lowered is not None:
                surviving.append(lowered)
            if lowered is None and loss is None:
                continue  # nothing to carry, so nothing is lost either
            if foreign_producer:
                dropped += 1
                _note(LOSS_FOREIGN_PRODUCER)
            elif lowered is None:
                dropped += 1
                _note(loss)
            elif not _is_representable(lowered, required_bindings):
                dropped += 1
                _note(LOSS_ITEM_UNREPRESENTABLE)
            elif loss is None or not binding_is_carried:
                # Either the block crossed whole, or the binding that was removed is
                # one this surface's wire has no field for: nothing it would have
                # carried was taken, so the readable text crosses at no cost.
                kept += 1
            else:
                stripped += 1
                _note(loss)
        if surviving:
            out.append({**message, "_provider_content": surviving})
        else:
            # Nothing was left to carry: drop the private list rather than leave an
            # empty one, so the lowering stage sees the shape it sees for a message
            # that never carried reasoning.
            out.append({key: value for key, value in message.items() if key != "_provider_content"})
    return out, ReasoningTransfer(
        kept=kept, stripped=stripped, dropped=dropped, losses=tuple(losses)
    )


# ─── Provider ───────────────────────────────────────────────────────────────


class _SwitchyardBoundary:
    """The crossing every Switchyard surface shares.

    Concrete adapters override ``create_streaming`` and call ``_lower_messages``
    first.  The split keeps one implementation of the crossing while leaving each
    surface's ``create_streaming`` a real override of the parent it inherits from,
    so a surface cannot bypass the classification by accident: skipping the call
    means handing the parent unfiltered reasoning, which the surface tests catch.

    The crossing outcome is published on ``last_reasoning_transfer``, held per
    calling thread rather than on the adapter: the factory hands out one shared
    instance per surface, so a second session crossing while the first is still
    between its call and its read would otherwise overwrite the first session's
    counts.  Every crossing happens and is read on the thread that drives the
    turn, which is what makes a thread-local slot the right scope.
    """

    _switchyard_surface = "responses"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._crossing_slot = threading.local()

    @property
    def provider_name(self) -> str:
        return PROVIDER_NAME

    @property
    def last_reasoning_transfer(self) -> ReasoningTransfer:
        """The calling thread's most recent crossing, empty when it has none."""
        return getattr(self._crossing_slot, "transfer", ReasoningTransfer())

    def _lower_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the list this surface should receive, recording what it cost.

        The outcome lands on ``self.last_reasoning_transfer`` (this thread's slot)
        for the caller to record alongside the turn.  The surface named by
        ``_switchyard_surface`` supplies the carrier policy the classification is
        read against; every surface the factory can build has a row.
        """
        carrier = _SURFACE_CARRIERS[self._switchyard_surface]
        lowered, transfer = retain_transferable_reasoning(
            messages,
            required_bindings=carrier.required_bindings,
            native_producer=carrier.native_producer,
            binding_is_carried=carrier.binding_is_carried,
        )
        self._crossing_slot.transfer = transfer
        return lowered


class SwitchyardResponsesProvider(_SwitchyardBoundary, OpenAIResponsesProvider):
    """Switchyard's Responses surface (``api_surface="responses"``).

    Operator-owned capabilities: Switchyard declares what its fleet serves, and a
    commercial capability table does not apply to a lane the operator owns.  That
    is the parent's ``compat`` mode, kept as the default here.
    """

    _switchyard_surface = "responses"

    def __init__(self, *, compat: bool = True) -> None:
        super().__init__(compat=compat)

    def create_streaming(
        self, *, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Iterator[StreamChunk]:
        # ``**kwargs`` rather than a copy of the parent's parameter list: the
        # crossing has no opinion about the rest of the request, and a duplicated
        # signature would go stale the first time the parent gains a parameter.
        return super().create_streaming(messages=self._lower_messages(messages), **kwargs)


class SwitchyardChatProvider(_SwitchyardBoundary, OpenAIChatCompletionsProvider):
    """Switchyard's Chat Completions surface (``api_surface="chat"``).

    The surface the production lanes ride today; the crossing is identical to the
    Responses class, so a lane can move between surfaces without changing what the
    ledger can carry.
    """

    _switchyard_surface = "chat"

    def __init__(self) -> None:
        super().__init__()

    def create_streaming(
        self, *, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Iterator[StreamChunk]:
        return super().create_streaming(messages=self._lower_messages(messages), **kwargs)
