import logging
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from openai import APIConnectionError, RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_chatter,
    get_embedder,
    get_pipeline_context,
    get_search,
    get_session,
)
from app.pipeline.context import PipelineContext
from app.repositories import datasets as datasets_repo
from app.repositories import documents as documents_repo
from app.schemas.query import (
    QueryRequest,
    QueryResponse,
    SourceFileInfo,
    TokenUsageInfo,
)
from app.services.chat import Chatter
from app.services.embeddings import Embedder
from app.services.search_index import SearchGateway

router = APIRouter(tags=["query"])
_LOG = logging.getLogger("app.query")


def _raw_score(row: dict[str, Any]) -> float:
    reranker = row.get("@search.reranker_score")
    if isinstance(reranker, (int, float)):
        return float(reranker)
    score = row.get("@search.score", 0.0)
    try:
        return float(score)
    except (TypeError, ValueError):
        return 0.0


def _to_percentage(score: float, max_score: float) -> float:
    """Project a raw score into a 0-100 percentage relative to the highest
    score in this result set. Reranker scores nominally cap at ~4, plain
    BM25/vector scores can go higher; normalizing against this query's max
    gives a stable relative-relevance reading."""
    if max_score <= 0:
        return 0.0
    return round(min(100.0, (score / max_score) * 100.0), 2)


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest,
    session: AsyncSession = Depends(get_session),
    ctx: PipelineContext = Depends(get_pipeline_context),
    embedder: Embedder = Depends(get_embedder),
    chatter: Chatter = Depends(get_chatter),
    search_gw: SearchGateway = Depends(get_search),
) -> QueryResponse:
    started = time.perf_counter()
    query_preview = body.query[:120].replace("\n", " ")
    _LOG.info(
        "query: %r (dataset=%s, use_cache=%s)",
        query_preview,
        body.dataset,
        body.use_cache,
    )

    dataset_uuid: uuid.UUID | None = None
    dataset_name: str | None = None
    if body.dataset:
        try:
            dataset_uuid = uuid.UUID(body.dataset)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Dataset ID '{body.dataset}' not found. Please use a valid dataset ID from GET /datasets.",
            )
        ds = await datasets_repo.get(session, dataset_uuid)
        if ds is None:
            raise HTTPException(
                status_code=400,
                detail=f"Dataset ID '{body.dataset}' not found. Please use a valid dataset ID from GET /datasets.",
            )
        dataset_name = ds.name

    # Priority lane: never queue the user's query behind bulk-ingest embedding
    # batches. Fails fast under heavy throttling instead of hanging ~40s.
    try:
        query_vec = await embedder.embed_query(body.query)
    except (RateLimitError, APIConnectionError) as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Embedding service is busy (likely a large ingest in "
                "progress). Please retry in a moment."
            ),
        ) from exc

    rows = await search_gw.hybrid_search(
        body.query,
        query_vec,
        top_k=ctx.settings.search_top_k_chunks,
        dataset_id=dataset_uuid,
    )
    _LOG.info("query retrieved %d chunks from index", len(rows))

    if not rows:
        return QueryResponse(
            query=body.query,
            answer="No matching documents.",
            from_cache=False,
            files=[],
            dataset=body.dataset,
            token_usage=None,
        )

    by_doc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"doc_name": "", "score_max": float("-inf"), "chunks": []}
    )
    for row in rows:
        did = row["doc_id"]
        s = _raw_score(row)
        by_doc[did]["doc_name"] = row["doc_name"]
        if s > by_doc[did]["score_max"]:
            by_doc[did]["score_max"] = s
        by_doc[did]["chunks"].append(
            {
                "chunk_index": row["chunk_index"],
                "content": row.get("content", ""),
                "score": s,
            }
        )

    ranked_doc_ids = sorted(
        by_doc.keys(), key=lambda d: by_doc[d]["score_max"], reverse=True
    )[: ctx.settings.search_top_k_docs]

    all_chunks: list[dict[str, Any]] = []
    for did in ranked_doc_ids:
        for c in by_doc[did]["chunks"]:
            all_chunks.append(
                {
                    "doc_id": did,
                    "doc_name": by_doc[did]["doc_name"],
                    "chunk_index": c["chunk_index"],
                    "content": c["content"],
                    "score": c["score"],
                }
            )
    all_chunks.sort(key=lambda c: c["score"], reverse=True)
    contexts = all_chunks[: ctx.settings.chat_max_context_chunks]

    answer_text, usage = await chatter.answer(body.query, contexts)
    _LOG.info(
        "query served in %.1fms (chat ctx: %d chunks)",
        (time.perf_counter() - started) * 1000.0,
        len(contexts),
    )

    # Enrich each source with dataset_id / dataset_name. Bulk-load to avoid
    # one DB hit per source.
    docs = await documents_repo.bulk_get(session, ranked_doc_ids)
    doc_map = {d.id: d for d in docs}
    ds_names: dict[uuid.UUID, str] = {}
    if dataset_name and dataset_uuid:
        ds_names[dataset_uuid] = dataset_name
    needed_ds_ids = {
        d.dataset_id for d in docs if d.dataset_id and d.dataset_id not in ds_names
    }
    for ds_id in needed_ds_ids:
        ds_row = await datasets_repo.get(session, ds_id)
        if ds_row is not None:
            ds_names[ds_id] = ds_row.name

    max_score = max((by_doc[d]["score_max"] for d in ranked_doc_ids), default=0.0)
    sources: list[SourceFileInfo] = []
    for did in ranked_doc_ids:
        doc = doc_map.get(did)
        doc_ds_id = doc.dataset_id if doc else None
        sources.append(
            SourceFileInfo(
                id=did,
                name=by_doc[did]["doc_name"],
                relevance_score=_to_percentage(by_doc[did]["score_max"], max_score),
                dataset_id=str(doc_ds_id) if doc_ds_id else None,
                dataset_name=ds_names.get(doc_ds_id) if doc_ds_id else None,
            )
        )

    return QueryResponse(
        query=body.query,
        answer=answer_text,
        from_cache=False,
        files=sources,
        dataset=body.dataset,
        token_usage=TokenUsageInfo(**usage),
    )
