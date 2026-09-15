"""Immutable, validated content handed from preparation to a short commit."""

import math
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any


class FrozenDict(dict):
    """JSON-compatible dictionary with no mutation operations."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("prepared import is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if is_dataclass(value):
        return replace(
            value, **{item.name: freeze(getattr(value, item.name)) for item in fields(value)}
        )
    if isinstance(value, dict):
        return FrozenDict((key, freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PreparedImport:
    document: Any
    embeddings: FrozenDict
    embedding_model: str | None

    @classmethod
    def build(cls, document, embeddings, embedding_model=None):
        dimensions = set()
        for vectors in embeddings.values():
            for vector in vectors:
                if not vector or any(not math.isfinite(float(value)) for value in vector):
                    raise ValueError("prepared embeddings must be finite nonempty vectors")
                dimensions.add(len(vector))
        if len(dimensions) > 1:
            raise ValueError("prepared embeddings have inconsistent dimensions")
        if embeddings and not embedding_model:
            raise ValueError("prepared embeddings require an immutable model identity")
        return cls(freeze(document), freeze(embeddings), embedding_model)
