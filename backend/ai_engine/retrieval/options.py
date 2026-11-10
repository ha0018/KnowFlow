from dataclasses import asdict, dataclass, replace

from django.conf import settings


@dataclass(frozen=True)
class RetrievalOptions:
    mode: str = "hybrid"
    candidate_k: int = 40
    rerank_k: int = 20
    per_document: int = 2
    rrf_constant: int = 60
    rerank: bool = False
    rewrite: bool = False
    vector_index: str = "exact"
    ef_search: int = 100

    def validate(self):
        if (
            self.mode not in {"dense", "hybrid"}
            or self.vector_index not in {"exact", "hnsw"}
            or type(self.rerank) is not bool
            or type(self.rewrite) is not bool
            or any(
                type(v) is not int
                for v in (
                    self.candidate_k,
                    self.rerank_k,
                    self.per_document,
                    self.rrf_constant,
                    self.ef_search,
                )
            )
            or not 5 <= self.candidate_k <= 100
            or not 5 <= self.rerank_k <= self.candidate_k
            or not 1 <= self.per_document <= 8
            or not 1 <= self.rrf_constant <= 1000
            or not 5 <= self.ef_search <= 1000
        ):
            raise ValueError("RETRIEVAL_CONFIG_INVALID")
        return self

    def snapshot(self):
        return {
            "version": "rrf-v2",
            **asdict(self),
            "rewrite_provider": settings.REWRITE_PROVIDER,
            "reranker_provider": settings.RERANKER_PROVIDER,
            "reranker_model": settings.RERANKER_MODEL,
            "reranker_max_chars": settings.RERANKER_MAX_CHARS,
        }


def defaults():
    return RetrievalOptions(
        rerank=settings.RETRIEVAL_RERANK_ENABLED,
        rewrite=settings.RETRIEVAL_REWRITE_ENABLED,
        vector_index=settings.RETRIEVAL_VECTOR_INDEX,
    ).validate()


def baseline(name, *, rewrite=False):
    if name not in {"B0", "B1", "B2"}:
        raise ValueError("BASELINE_INVALID")
    return replace(
        RetrievalOptions(), mode="dense" if name == "B0" else "hybrid", rerank=name == "B2", rewrite=rewrite
    ).validate()
