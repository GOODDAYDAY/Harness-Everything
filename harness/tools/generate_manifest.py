"""Generate harness/tools/registry_manifest.json from live tool lists.

Run:  python -m harness.tools.generate_manifest
"""
import json
import pathlib

from harness.tools import DEFAULT_TOOLS, OPTIONAL_TOOLS

_OUT = pathlib.Path(__file__).parent / "registry_manifest.json"

manifest = {
    "generated_by": "harness.tools.generate_manifest",
    "default_tools": [t.name for t in DEFAULT_TOOLS],
    "optional_tools": [t.name for t in OPTIONAL_TOOLS],
    "default_count": len(DEFAULT_TOOLS),
    "optional_count": len(OPTIONAL_TOOLS),
}

_OUT.write_text(json.dumps(manifest, indent=2) + "\n")
print(
    f"Wrote {_OUT} "
    f"({manifest['default_count']} default, {manifest['optional_count']} optional tools)"
)

if __name__ == "__main__":
    pass  # execution happens at module level for -m compatibility
