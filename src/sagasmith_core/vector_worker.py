"""Run one bounded delivery batch; a host scheduler controls repetition."""

import argparse
import json
from dataclasses import asdict

from sagasmith_core.database import Database
from sagasmith_core.embeddings import EMBEDDING_PROFILES
from sagasmith_core.vector import VectorStore
from sagasmith_core.vector_jobs import VectorIndexJobService


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--system-id", required=True)
    parser.add_argument("--collection", choices=("rules", "modules"), required=True)
    parser.add_argument("--profile", choices=tuple(EMBEDDING_PROFILES), required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    database = Database(args.database_url)
    try:
        service = VectorIndexJobService(database)
        if args.status:
            result = service.status(system_id=args.system_id, collection=args.collection)
        else:
            profile = EMBEDDING_PROFILES[args.profile]
            result = asdict(
                service.flush(
                    VectorStore(args.system_id),
                    system_id=args.system_id,
                    collection=args.collection,
                    embedding_model=profile.storage_model_id,
                    profile=profile,
                    limit=args.limit,
                )
            )
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get("failed", 0) else 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
