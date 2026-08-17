"""Role-scoped retrieval.

Two things happen here, and keeping them separate is the point:

1. **Filtering** decides what a caller is permitted to see. It runs as a SQL
   predicate, so documents outside the caller's scope never enter the process.
2. **Ranking** decides what is most relevant among those permitted rows.

Filtering is security and is not negotiable. Ranking is quality and can be
swapped -- keyword overlap here, pgvector similarity once embeddings are wired
up -- without touching the security boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import Document, Role

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Hit:
    document_id: int
    title: str
    snippet: str
    score: float


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def permitted_documents(role: Role):
    """SELECT restricted to documents this role may read.

    Everything downstream operates on the result of this statement, so there is
    no path by which an out-of-scope document reaches a ranker, a prompt, or a
    log line.
    """
    return select(Document).where(Document.allowed_roles.like(f"%,{role.value},%"))


def search(session: Session, role: Role, query: str, limit: int) -> list[Hit]:
    """Rank the documents this role may read against a query."""
    candidates = session.scalars(permitted_documents(role)).all()
    q = _tokens(query)
    if not q:
        return []

    hits: list[Hit] = []
    for doc in candidates:
        body_tokens = _tokens(doc.title + " " + doc.body)
        overlap = len(q & body_tokens)
        if not overlap:
            continue
        hits.append(
            Hit(
                document_id=doc.id,
                title=doc.title,
                snippet=_snippet(doc.body, q),
                # Normalised by query length so longer documents do not win by
                # sheer size.
                score=round(overlap / len(q), 3),
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def _snippet(body: str, query_tokens: set[str]) -> str:
    """Return the sentence with the most query-token overlap."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
    if not sentences:
        return body[:200]
    best = max(sentences, key=lambda s: len(_tokens(s) & query_tokens))
    return best
