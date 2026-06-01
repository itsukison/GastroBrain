import cohere
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from gastrobrain.config import settings

_client: cohere.ClientV2 | None = None


def _get_client() -> cohere.ClientV2:
    global _client
    if _client is None:
        # 60s per request — catches ghosted TCP connections (laptop sleep, NAT timeout)
        # so the retry decorator can re-establish instead of hanging forever.
        _client = cohere.ClientV2(api_key=settings.cohere_api, timeout=60.0)
    return _client


_RETRYABLE_EMBED = (
    cohere.errors.TooManyRequestsError,
    cohere.errors.InternalServerError,
    httpx.TransportError,  # covers RemoteProtocolError, ConnectError, ReadError, etc.
)


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=65),
    retry=retry_if_exception_type(_RETRYABLE_EMBED),
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
    retry=retry_if_exception_type(_RETRYABLE_EMBED),
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
