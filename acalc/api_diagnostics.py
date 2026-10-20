# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Resolve API diagnostic identities using the submitted model snapshot."""

import re


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_IDENTITY = re.compile(
    rf"(?<![\w/-])(?:building:|barrier:)?(?P<id>{_UUID}/{_UUID}/{_UUID}(?:/source/{_UUID})?)(?![\w-])",
    re.IGNORECASE,
)


def named_diagnostic(message, snapshot):
    """Change display text only; preserve request IDs and raw diagnostics."""
    if not snapshot:
        return message
    mapping = snapshot.get("id_mapping", {})
    layers = {layer["layer_id"]: layer["name"]
              for layer in snapshot.get("provenance", {}).get("layers", [])}

    def replace(match):
        reference = mapping.get(match["id"])
        if not reference:
            return match[0]
        kind = reference.get("kind", "object").replace("_", " ").capitalize()
        name = " ".join(reference.get("name", "").split())
        label = f'{kind} “{name}”' if name else f"Unnamed {kind.lower()}"
        location = []
        layer = reference.get("layer_name") or layers.get(reference.get("layer_id"))
        if layer:
            location.append('layer “' + " ".join(layer.split()) + '”')
        if type(reference.get("fid")) is int:
            location.append(f"feature {reference['fid']}")
        if location:
            label += " (" + ", ".join(location) + ")"
        return label

    return _IDENTITY.sub(replace, message)
