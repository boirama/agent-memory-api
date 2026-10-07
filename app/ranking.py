"""Hybrid ranking — what makes this better than ORDER BY embedding <=> query.

Three signals combine:

  semantic (0.55)  cosine similarity against the query embedding
  lexical  (0.25)  Postgres ts_rank_cd over the generated tsvector
  salience (0.20)  base importance, decayed by time since last access and
                   reinforced by how often the memory has been recalled

The salience term is the interesting one. Vector stores treat every memory
as equally valuable forever. Real agent memory shouldn't: a fact recalled
forty times is load-bearing, a fact untouched in six months probably isn't.
Decay is exponential on days-since-access; reinforcement is logarithmic on
access count so a hot memory saturates rather than running away.

All the maths happens in Postgres in one round trip. Doing it in Python
means pulling candidate rows over the wire first, which is where naive
implementations lose their latency budget.
"""

from __future__ import annotations

import re

from .config import settings

_HIGH_SIGNAL = re.compile(
    r"\b(always|never|prefer|prefers|preferred|require|requires|must|"
    r"critical|important|policy|deadline|contract|budget|allerg|"
    r"decision|decided|agreed|owner|responsible|password|credential|"
    r"key contact|escalat)\w*\b",
    re.IGNORECASE,
)
_LOW_SIGNAL = re.compile(
    r"\b(maybe|perhaps|might|possibly|unsure|temporarily|for now|"
    r"draft|placeholder|test|todo)\w*\b",
    re.IGNORECASE,
)


def infer_importance(content: str) -> float:
    """Cheap, explainable heuristic for when the caller omits importance.

    Deliberately not an LLM call: this runs on every write, and paying for
    inference on the write path would blow the $0.001 unit economics.
    """
    score = 0.5

    if _HIGH_SIGNAL.search(content):
        score += 0.22
    if _LOW_SIGNAL.search(content):
        score -= 0.18

    length = len(content)
    if length > 400:
        score += 0.08
    elif length < 40:
        score -= 0.08

    # Concrete detail - dates, money, identifiers - signals a real fact.
    if re.search(r"\d{4}-\d{2}-\d{2}|\$\s?\d|\b\d{3,}\b|@\w+\.\w+", content):
        score += 0.10

    if content.strip().endswith("?"):
        score -= 0.12

    return round(min(1.0, max(0.05, score)), 4)


# A memory is visible if the agent owns it, a live namespace grant covers
# it, or it was directly shared. Grants are hierarchical: a grant on
# 'acme/sales' covers 'acme/sales/leads'.
_VISIBILITY_SQL = """
(
    m.owner_agent_id = :agent_id
    OR EXISTS (
        SELECT 1 FROM mesh_grants g
        WHERE g.tenant_id = m.tenant_id
          AND g.granter_agent_id = m.owner_agent_id
          AND g.grantee_agent_id IN (:agent_id, '*')
          AND (m.namespace = g.namespace OR m.namespace LIKE g.namespace || '/%')
          AND g.revoked_at IS NULL
          AND (g.expires_at IS NULL OR g.expires_at > now())
    )
    OR EXISTS (
        SELECT 1 FROM mesh_shares s
        WHERE s.tenant_id = m.tenant_id
          AND s.memory_id = m.id
          AND s.to_agent_id = :agent_id
          AND s.revoked_at IS NULL
          AND (s.expires_at IS NULL OR s.expires_at > now())
    )
)
"""

_OWNED_ONLY_SQL = "m.owner_agent_id = :agent_id"

_VIA_SQL = """
CASE
    WHEN m.owner_agent_id = :agent_id THEN 'owned'
    WHEN EXISTS (
        SELECT 1 FROM mesh_shares s
        WHERE s.tenant_id = m.tenant_id AND s.memory_id = m.id
          AND s.to_agent_id = :agent_id AND s.revoked_at IS NULL
          AND (s.expires_at IS NULL OR s.expires_at > now())
    ) THEN 'share'
    ELSE 'grant'
END AS via
"""


def build_recall_sql(
    *,
    include_shared: bool,
    namespace_filter: bool = False,
    namespace_list_filter: bool = False,
    scope_filter: bool = False,
    subject_filter: bool = False,
    time_window_filter: bool = False,
) -> str:
    """Assemble the recall query.

    Built as a string rather than the SQLAlchemy expression API because
    pgvector operators and ts_rank_cd have no clean ORM equivalent, and
    ORM-generated SQL is harder to EXPLAIN when tuning. Every user value
    is still a bound parameter - nothing is interpolated.
    """
    visibility = _VISIBILITY_SQL if include_shared else _OWNED_ONLY_SQL

    clauses = [
        "m.tenant_id = :tenant_id",
        "m.deleted_at IS NULL",
        "(m.expires_at IS NULL OR m.expires_at > now())",
        "m.superseded_by IS NULL",
        visibility,
    ]
    if namespace_filter:
        clauses.append("(m.namespace = :namespace OR m.namespace LIKE :namespace_prefix)")
    if namespace_list_filter:
        clauses.append("m.namespace = ANY(:namespaces)")
    if scope_filter:
        clauses.append("m.scope = :scope")
    if subject_filter:
        clauses.append("m.subject_id = :subject_id")
    if time_window_filter:
        clauses.append("m.created_at >= :window_start AND m.created_at <= :window_end")

    where = "\n      AND ".join(clauses)

    return f"""
WITH candidates AS (
    SELECT
        m.id, m.content, m.owner_agent_id, m.scope, m.namespace, m.subject_id,
        m.importance, m.access_count, m.created_at, m.updated_at,
        m.expires_at, m.metadata,
        1.0 - (m.embedding <=> CAST(:query_vec AS vector)) AS semantic_score,
        ts_rank_cd(m.search_vector, websearch_to_tsquery('english', :query_text)) AS raw_lexical,
        EXP(
            -CAST(:decay_rate AS double precision)
            * (EXTRACT(EPOCH FROM (now() - m.last_accessed_at)) / 86400.0)
        ) AS decay,
        {_VIA_SQL}
    FROM memories m
    WHERE {where}
),
scored AS (
    SELECT
        c.*,
        -- ts_rank_cd is unbounded; squash to 0..1 so weights mean what
        -- they say they mean.
        LEAST(1.0, c.raw_lexical * 4.0) AS lexical_score,
        LEAST(
            1.0,
            c.importance * c.decay
            + CAST(:reinforcement AS double precision) * LN(1 + c.access_count)
        ) AS salience_score
    FROM candidates c
)
SELECT
    s.*,
    (
          CAST(:w_semantic AS double precision) * GREATEST(0.0, s.semantic_score)
        + CAST(:w_lexical  AS double precision) * s.lexical_score
        + CAST(:w_salience AS double precision) * s.salience_score
    ) AS score
FROM scored s
WHERE s.salience_score >= CAST(:min_importance AS double precision)
ORDER BY score DESC, s.created_at DESC
LIMIT :limit
"""


def base_params() -> dict:
    return {
        "decay_rate": settings.decay_rate_per_day,
        "reinforcement": settings.reinforcement_factor,
        "w_semantic": settings.weight_semantic,
        "w_lexical": settings.weight_lexical,
        "w_salience": settings.weight_salience,
    }


# Called after a successful recall. Reinforcement is what makes salience
# adaptive instead of a field the caller sets once and forgets.
REINFORCE_SQL = """
UPDATE memories
   SET access_count = access_count + 1,
       last_accessed_at = now()
 WHERE id = ANY(CAST(:ids AS uuid[]))
"""
