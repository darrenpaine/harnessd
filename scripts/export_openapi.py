"""Write the app's OpenAPI schema to docs/openapi.json (SPEC-0007 B-0006)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "openapi.json"


def render(schema: dict[str, object]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from app import app

    OUTPUT.write_text(render(app.openapi()))
    print(f"wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
