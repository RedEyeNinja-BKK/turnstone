"""Compatibility helpers for driving the shadow-sensor seams on Turnstone 1.9.4.

The 1.8.7 sensor tests were written against two ``ChatSession`` methods that
1.9.4 removed or changed:

``_append_pending_system_turns(specs, deferred_persistence=...)``
    1.9.4 inlined this drain into ``send()``'s tool-batch fold: it iterates
    ``pending_system_turns`` and calls ``_append_system_turn`` per entry. This
    module reproduces that loop verbatim (including the sensor seam) so the
    tests exercise the SAME code path shape the product uses.

``_collect_advisories(assessment, func_name, is_last_in_batch=...)``
    1.9.4 replaced ``is_last_in_batch`` with ``received`` / ``result_index`` /
    ``result_count``. This module adapts the old call to the new signature.

Both helpers are test-only. They change no product code and assert the same
behaviours the original tests asserted.
"""

from __future__ import annotations

from typing import Any


def collect_advisories(
    session: Any,
    *,
    assessment: Any = None,
    func_name: str = "bash",
) -> list[tuple[str, str, dict[str, Any]]]:
    """Call ``_collect_advisories`` across both SDK generations.

    1.8.7: ``(assessment, func_name, is_last_in_batch)``.
    1.9.4: ``(assessment, func_name, *, received, result_index, result_count)``.
    """
    import inspect

    sig = inspect.signature(session._collect_advisories)
    if "is_last_in_batch" in sig.parameters:
        return session._collect_advisories(
            assessment=assessment, func_name=func_name, is_last_in_batch=True
        )
    # 1.9.4: one result, and the LAST in its batch. The product computes
    # ``is_last_in_batch = result_index == result_count`` with result_index
    # from 1, so a single-result batch is index 1 of 1.
    return session._collect_advisories(
        assessment,
        func_name,
        received=None,
        result_index=1,
        result_count=1,
    )


def append_pending_system_turns(
    session: Any,
    specs: list[tuple[str, str, dict[str, Any]]],
    *,
    deferred_persistence: list[Any] | None = None,
) -> None:
    """Reproduce 1.9.4's inline pending-system-turn drain, seam included.

    In 1.9.4 ``send()`` does this inline; there is no method to call. The loop
    body below is a faithful copy of the product code at that site, including
    the advisory sensor seam, so a test driving it measures the same thing the
    product does.

    If a future version restores a named method, prefer it: this shim exists
    only because the product inlined the drain.
    """
    if deferred_persistence is None:
        deferred_persistence = []

    # Prefer a real product method if one exists (1.8.7, or a future 1.9.x).
    real = getattr(session, "_append_pending_system_turns", None)
    if callable(real):
        real(specs, deferred_persistence=deferred_persistence)
        return

    from turnstone.core.session import _sensor_observe_substantive, _sensor_turn_identity

    for source, content, meta in specs:
        # Product order is append-then-observe: the seam sits AFTER
        # ``_append_system_turn`` because it reports the identity of the row
        # that admission just created. A failed append therefore produces no
        # observation at all -- the operator's text never reached the model.
        session._append_system_turn(
            source,
            content,
            deferred_persistence=deferred_persistence,
            **meta,
        )
        if source == "user_interjection":
            _sensor_observe_substantive(
                meta.get("message", ""),
                workstream=getattr(session, "_ws_id", ""),
                registry=getattr(session, "_registry", None),
                turn_id=_sensor_turn_identity(session, session._generation),
            )