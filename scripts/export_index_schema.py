"""Print the Azure AI Search index schema as JSON.

`build_index()` in app/services/search_index.py is the single source of truth.
This script serializes it for anyone who wants to PUT the schema manually
(e.g. when ENSURE_INDEX_ON_STARTUP=false) without booting the whole app.

    python scripts/export_index_schema.py > index.json
"""
import json
import sys

from app.services.search_index import build_index
from app.settings import get_settings


def main() -> int:
    settings = get_settings()
    index = build_index(
        settings.azure_search_index,
        enable_semantic=settings.enable_semantic_ranking,
        dimensions=settings.embedding_dimensions,
    )
    json.dump(index.serialize(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
