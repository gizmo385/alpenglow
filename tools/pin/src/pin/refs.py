"""Parse and build image references.

An image reference is ``name[:tag][@digest]``. The name keeps any registry
host and port (``host:5000/team/app``), so the tag is the part after the first
colon in the *last* path segment. That also keeps env-interpolated tags such as
``ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}`` in one piece.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Tags that name a moving channel rather than a release. A pinned entry may
# not use one of these.
FLOATING_TAGS = frozenset(
    {"latest", "stable", "main", "master", "release", "edge", "nightly", "dev", "beta", "rc"}
)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# The leading version of a tag: "2.11.7" in "2.11.7-builder", "24" in "24-slim".
_VERSION_PREFIX_RE = re.compile(r"^v?\d+(?:\.\d+)*")

# A full release: "v1.21.3", "8.10.3-alpine". Not "2", "12.2" or "alpine", which
# keep moving as new releases come out.
_RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+")


@dataclass(frozen=True)
class ImageRef:
    name: str
    tag: str | None = None
    digest: str | None = None

    @property
    def interpolated(self) -> bool:
        """True when the reference depends on an environment variable."""
        return "$" in self.name or "$" in (self.tag or "")

    def __str__(self) -> str:
        return format_ref(self.name, self.tag, self.digest)


def parse_ref(ref: str) -> ImageRef:
    ref = ref.strip()
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    if "/" in ref:
        prefix, last = ref.rsplit("/", 1)
        prefix += "/"
    else:
        prefix, last = "", ref
    tag = None
    if ":" in last:
        last, tag = last.split(":", 1)
    return ImageRef(prefix + last, tag or None, digest or None)


def format_ref(name: str, tag: str | None, digest: str | None = None) -> str:
    out = name
    if tag:
        out += f":{tag}"
    if digest:
        out += f"@{digest}"
    return out


def is_release_tag(tag: str | None) -> bool:
    return bool(tag and _RELEASE_TAG_RE.match(tag) and "$" not in tag)


def split_variant(tag: str) -> tuple[str, str]:
    """Split a tag into its version and variant suffix.

    ``2.11.7-builder`` -> (``2.11.7``, ``-builder``), ``3.13-slim`` ->
    (``3.13``, ``-slim``), ``17.10`` -> (``17.10``, ``""``). A tag without a
    leading version (``alpine``) comes back as (``alpine``, ``""``).
    """
    m = _VERSION_PREFIX_RE.match(tag)
    if not m:
        return tag, ""
    return m.group(0), tag[m.end():]
