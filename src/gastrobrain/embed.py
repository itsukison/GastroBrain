import cohere
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gastrobrain.config import settings

_client: cohere.ClientV2 | None = None


def _get_client() -> cohere.ClientV2:
    global _client
    if _client is None:
        _client = cohere.ClientV2(api_key=settings.cohere_api)
    return _client


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type((cohere.errors.TooManyRequestsError, cohere.errors.InternalServerError)),
    reraise=True,
)
def embed_texts(texts: list[str], input_type: str) -> list[list[float]]:
    """Cohere multilingual-v3 embedding.

    input_type: "search_document" for chunks at ingest, "search_query" for user questions.
    """
    if not texts:
        return []
    resp = _get_client().embed(
        model=settings.embedding_model,
        texts=texts,
        input_type=input_type,
        embedding_types=["float"],
    )
    return resp.embeddings.float_  # type: ignore[union-attr]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((cohere.errors.TooManyRequestsError, cohere.errors.InternalServerError)),
    reraise=True,
)
def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    resp = _get_client().rerank(
        model=settings.rerank_model,
        query=query,
        documents=documents,
        top_n=top_n,
    )
    return [(r.index, r.relevance_score) for r in resp.results]
