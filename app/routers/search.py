import logging
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_chatter, get_embedder, get_pipeline_context, get_search, get_session
from app.pipeline.context import PipelineContext
from app.repositories import datasets as datasets_repo
from app.schemas.search import SearchRequest, SearchResponse, SearchSource
from app.services.chat import Chatter
from app.services.embeddings import Embedder
from app.services.search_index import SearchGateway

router = APIRouter(tags=["search"])
_LOG = logging.getLogger("app.search")


def _score(row: dict[str, Any]) -> float:
    reranker = row.get("@search.reranker_score")
    if isinstance(reranker, (int, float)):
        return float(reranker)
    score = row.get("@search.score", 0.0)
    try:
        return float(score)
    except (TypeError, ValueError):
        return 0.0


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    session: AsyncSession = Depends(get_session),
    ctx: PipelineContext = Depends(get_pipeline_context),
    embedder: Embedder = Depends(get_embedder),
    chatter: Chatter = Depends(get_chatter),
    search_gw: SearchGateway = Depends(get_search),
) -> SearchResponse:
    started = time.perf_counter()
    query_preview = body.query[:120].replace("\n", " ")
    _LOG.info(
        "search query: %r (dataset=%s, top_k=%d)",
        query_preview,
        body.dataset_id,
        body.top_k,
    )

    if body.dataset_id is not None:
        ds = await datasets_repo.get(session, body.dataset_id)
        if ds is None:
            raise HTTPException(status_code=404, detail="dataset not found")

    vectors = await embedder.embed_many([body.query])
    if not vectors:
        raise HTTPException(status_code=500, detail="failed to embed query")
    query_vec = vectors[0]

    rows = await search_gw.hybrid_search(
        body.query,
        query_vec,
        top_k=ctx.settings.search_top_k_chunks,
        dataset_id=body.dataset_id,
    )
    _LOG.info("search retrieved %d chunks from index", len(rows))

    if not rows:
        _LOG.info("search produced no results")
        return SearchResponse(answer="No matching documents.", sources=[])

    # Collapse chunks into docs, taking the BEST (max) chunk score as the
    # doc's relevance score. Summing across chunks would let a doc with many
    # weakly-relevant chunks outrank a doc with one strongly-relevant chunk
    # — that's the wrong intuition for "which document answers the query?".
    by_doc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"doc_name": "", "score_max": float("-inf"), "chunks": []}
    )
    for row in rows:
        did = row["doc_id"]
        s = _score(row)
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
    )[: body.top_k]

    # Build chat context across the selected docs, highest-scoring chunks first.
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

    top_summary = ", ".join(
        f"{by_doc[d]['doc_name']}({by_doc[d]['score_max']:.2f})"
        for d in ranked_doc_ids
    )
    _LOG.info(
        "search ranked top %d docs: %s", len(ranked_doc_ids), top_summary
    )

    answer = await chatter.answer(body.query, contexts)
    _LOG.info(
        "search served in %.1fms (chat ctx: %d chunks)",
        (time.perf_counter() - started) * 1000.0,
        len(contexts),
    )

    sources = [
        SearchSource(
            doc_id=uuid.UUID(did),
            doc_name=by_doc[did]["doc_name"],
            score=float(by_doc[did]["score_max"]),
        )
        for did in ranked_doc_ids
    ]

    return SearchResponse(answer=answer, sources=sources)
