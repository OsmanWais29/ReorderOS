"""Dump the FastAPI OpenAPI schema to ``openapi.json``.

CI uses this as the artifact that drives the generated TypeScript client in Sprint 10.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app


def main() -> None:
    schema = app.openapi()
    out = Path("openapi.json")
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(schema['paths'])} paths)")


if __name__ == "__main__":
    main()
