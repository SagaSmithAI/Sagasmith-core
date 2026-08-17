"""System-neutral helpers for immutable content-package archive storage."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from sagasmith_core.content_pack import dumps_content_archive, loads_content_archive


def write_content_archive(
    directory: str | Path,
    package: dict[str, Any],
    blobs: dict[str, bytes],
) -> dict[str, Any]:
    """Write one deterministic package archive beneath a managed directory."""

    root = Path(directory).resolve()
    content = dumps_content_archive(package, blobs)
    checksum = str(package["checksum"])
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(package["id"])).strip("-.")
    filename = f"{checksum[:12]}-{safe_id}.sagasmith-pack"
    target = (root / filename).resolve()
    if target.parent != root:
        raise ValueError("invalid content package archive artifact name")
    if not target.exists():
        target.write_bytes(content)
    elif target.read_bytes() != content:
        raise RuntimeError("managed content package archive mismatch")
    return {
        "artifact": filename,
        "checksum": checksum,
        "archive_checksum": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "kind": package["kind"],
        "id": package["id"],
        "version": package["version"],
    }


def read_content_archive(
    directory: str | Path,
    *,
    artifact: str | None = None,
    source_path: str | Path | None = None,
    allowed_roots: Iterable[str | Path] = (),
    maximum_size: int = 4 * 1024 * 1024 * 1024,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read a managed or explicitly allowlisted content-package archive."""

    if (artifact is None) == (source_path is None):
        raise ValueError("provide exactly one of artifact or source_path")
    root = Path(directory).resolve()
    if artifact is not None:
        target = (root / artifact).resolve()
        if target.parent != root:
            raise ValueError("invalid managed content package artifact")
    else:
        target = Path(source_path or "").expanduser().resolve()
        roots = {Path(item).resolve() for item in allowed_roots}
        if not roots or not any(target.is_relative_to(item) for item in roots):
            raise PermissionError("content package is outside configured import roots")
    if not target.is_file() or not target.name.casefold().endswith(".sagasmith-pack"):
        raise LookupError(str(target))
    if target.stat().st_size > int(maximum_size):
        raise ValueError("content package exceeds the configured safety limit")
    return loads_content_archive(target.read_bytes())
