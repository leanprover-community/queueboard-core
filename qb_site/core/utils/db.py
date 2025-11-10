from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from django.db import IntegrityError, models, transaction


def _diff_fields(obj: models.Model, values: Dict[str, Any]) -> Tuple[bool, Tuple[str, ...]]:
    """Compare fields on an instance with provided values.

    Returns (changed: bool, updated_fields: tuple[str, ...]). Intended for simple
    scalar/FK fields only (no M2M). Callers should pre-normalize types (e.g.,
    timezone-aware datetimes) to avoid false diffs.
    """
    changed_fields: list[str] = []
    for field, value in values.items():
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed_fields.append(field)
    return (len(changed_fields) > 0, tuple(changed_fields))


def update_if_changed(
    obj: models.Model,
    values: Dict[str, Any],
    *,
    touch_updated_at: bool = True,
) -> Tuple[bool, Tuple[str, ...]]:
    """Set attributes on an instance and save only if any values differ.

    When to use:
    - You already have the instance and want idempotent writes that avoid no-op UPDATEs
      (helps keep ``updated_at`` meaningful and reduces DB churn).

    Notes
    - If ``touch_updated_at`` and the model has an ``updated_at`` field, it is added to
      ``update_fields`` on save; otherwise only changed fields are written.
    - Returns (updated: bool, updated_fields: tuple[str, ...]).
    """
    changed, fields = _diff_fields(obj, values)
    if not changed:
        return False, fields
    update_fields = list(fields)
    if touch_updated_at and hasattr(obj, "updated_at") and "updated_at" not in update_fields:
        update_fields.append("updated_at")
    obj.save(update_fields=update_fields)
    return True, fields


def upsert_if_changed(
    model: type[models.Model],
    lookup: Dict[str, Any],
    values: Dict[str, Any],
    *,
    touch_updated_at: bool = True,
) -> Tuple[models.Model, bool, bool, Tuple[str, ...]]:
    """Get or create a row by lookup and update only if values differ.

    When to use:
    - You need an idempotent upsert keyed by a small lookup (e.g., provider id) and
      want accurate (created/updated) metrics without no-op updates.

    Returns: (obj, created, updated, updated_fields).

    Notes
    - Uses a SELECT first; on create, wraps in a savepoint and retries SELECT on
      IntegrityError to tolerate rare races.
    - Avoids unnecessary UPDATEs so that auto-managed ``updated_at`` doesn't churn.
    - Not intended for bulk operations or M2M relations.
    """
    obj = model.objects.filter(**lookup).first()
    if obj is None:
        data = {**lookup, **values}
        try:
            with transaction.atomic():
                obj = model.objects.create(**data)
        except IntegrityError:
            # Another writer won the race: fall back to update path.
            obj = model.objects.get(**lookup)
            updated, fields = update_if_changed(obj, values, touch_updated_at=touch_updated_at)
            return obj, False, updated, fields
        return obj, True, False, tuple()

    updated, fields = update_if_changed(obj, values, touch_updated_at=touch_updated_at)
    return obj, False, updated, fields
