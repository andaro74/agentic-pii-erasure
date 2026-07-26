# ADR-021: S3 Vectors replaces OpenSearch Serverless — a cost decision

- **Status:** Accepted (amends the participant table in [ADR-017](ADR-017-real-aws-participants.md); **resolves ARCHITECTURE §16 Q7**)
- **Anchors invariants:** CLAUDE.md #5 (never log raw PII), #7 (residual honesty)
- **Baseline:** architecture v0.2 — AWS-native serverless

## Context

**This decision is about cost, and nothing else was wrong with the alternative.**

[ADR-017](ADR-017-real-aws-participants.md) chose **Amazon OpenSearch Serverless** as participant #6, the derived-index archetype. It is the right service for that archetype in the conventional sense — aliases, `_delete_by_query`, orphaned documents — and it worked.

It also has a **continuous OCU floor**. OpenSearch Serverless bills a minimum number of OpenSearch Compute Units for as long as the collection exists, independent of whether anything queries it. In a stack where every other component is per-request and scales to zero, that one participant dominated the bill by an order of magnitude over everything else combined — including Bedrock.

That produced a specific, damaging outcome: the cheapest way to use this repo was **not to deploy it**. `infra/README.md` opened with a red warning, the per-PR ephemeral eval stack ([ADR-020](ADR-020-deployed-eval-gate.md)) carried a cost per run that scaled with wall-clock rather than work, and a forgotten dev stack was an expensive mistake rather than a cheap one. For a reference architecture whose entire premise is "there is no local mode — deploy it" ([ADR-017](ADR-017-real-aws-participants.md)), a component that punishes deployment is a structural problem, not a line item.

ARCHITECTURE §16 Q7 recorded this as an open question: *is teaching the derived-index archetype worth a continuous charge?*

**Amazon S3 Vectors** answers it. It reached general availability in December 2025 and expanded to 17 additional regions in March 2026: native vector storage and similarity query in S3, priced on **storage per GB-month plus per-request**, with **no provisioned capacity and no idle floor**.

## Decision

**Participant #6 becomes `vector-index`, backed by Amazon S3 Vectors.** The reason is cost.

| | Before — OpenSearch Serverless | After — S3 Vectors |
|---|---|---|
| Idle cost | **Continuous OCU floor**, billed while the collection exists | **None.** Storage per GB-month + per-request only |
| Cost driver | Wall-clock time the collection exists | Vectors stored and requests made |
| A forgotten dev stack | Expensive | Cents |
| Per-PR eval stack | Cost scales with run duration | Cost scales with work done |
| Scales to zero | No | **Yes** |

The consequence at the stack level is the headline: **there is no longer any component in this architecture that bills continuously.** Lambda, DynamoDB on-demand, EventBridge Scheduler, S3, S3 Vectors, KMS, SES, Cognito, Glue/Athena, and AgentCore Runtime are all per-request or per-GB; Aurora Serverless v2 runs at `min_capacity = 0` ACU and leaves only storage. An idle stack now costs a few cents a month instead of hundreds of dollars.

## The archetype still works, but it teaches something different

This is a swap of one real service for another, so the lesson changes with it. Both directions are worth stating plainly rather than claiming a pure win.

**What is lost.** OpenSearch's derived-index lessons were about *documents*: alias exclusion as a soft delete, `_delete_by_query` as a hard delete, reindexing as recovery. S3 Vectors has none of those. If your mental model of "derived store" is a full-text search cluster, this participant no longer models it.

**What is gained, and why it fits this repo better.**

1. **There is no delete-by-query.** `DeleteVectors` takes keys — up to 500 per call — and nothing else. The participant must therefore be *able to enumerate every vector belonging to a subject*, which it does by deriving vector keys deterministically from the pseudonymous `subjectRef` rather than relying on a side mapping table. That is a real constraint the service imposes, and it sharpens §5.2's ordering rule: **keep the identifier alive until last**, because in this store losing the join key does not merely make deletion hard, it makes the orphaned vectors unfindable while remaining fully present.

2. **The derived artifact is an embedding, and an embedding of a subject's text is personal data.** It is a lossy representation from which content can be partially reconstructed. Deleting the source row in `profile-store` does not delete its embedding, and a reader who never touches the source can still retrieve a semantically faithful trace of the subject. The orphan-document lesson becomes an orphan-*embedding* lesson, which is strictly more uncomfortable and strictly more current.

3. **Soft delete has no alias to hide behind.** `PutVectors` upserts by key, so a soft delete is a re-put with a `deleted` flag in filterable metadata, and it only works if **every** query path applies the filter. A soft delete that depends on all readers remembering a `WHERE` clause is exactly the derived-index hazard this archetype exists to teach — S3 Vectors just refuses to let us paper over it with an alias.

4. **Vector metadata is a PII surface.** Up to 40 KB per vector, and it is tempting to stash the source text there. Invariant #5 applies to it exactly as it applies to logs: metadata goes through `observability/redact.py`, and only the pseudonymous handle and the classification survive.

For a reference architecture about *agentic* deletion, a RAG-shaped derived store is closer to the systems readers are actually building than a full-text cluster is. That is a genuine benefit — but it is not why this decision was made. **It was made on cost.**

## Consequences

- **Positive — the stack scales to zero.** The primary result, and the reason for the change.
- **Positive — deploying the repo stops being the expensive choice.** `infra/README.md` no longer has to open with a warning about a component that bills while you sleep; the remaining teardown hazard is the Object Lock retention period, which is a *correctness* constraint the architecture is deliberately teaching rather than an accident of pricing.
- **Positive — CI cost now scales with work, not with wall-clock.** The per-PR ephemeral stack ([ADR-020](ADR-020-deployed-eval-gate.md)) gets meaningfully cheaper, which makes the deployed gate easier to defend against the pressure to mock it.
- **Cost 1 — the archetype teaches a different lesson.** Documented above. A reader looking for full-text search semantics will not find them here.
- **Cost 2 — latency profile.** Infrequent queries return in under a second; frequently-queried indexes settle around 100 ms. That is fine for `discover` and for the T+7/T+30 verification sweeps, which are the only paths this repo uses. It is **not** a low-latency serving store, and a production system needing single-digit-millisecond search would use the documented tiering pattern (S3 Vectors for the cold corpus, OpenSearch for the hot tier) — which reintroduces the OCU floor for the hot tier. Say so rather than implying S3 Vectors replaces OpenSearch everywhere.
- **Cost 3 — hard API limits to design against.** 500 vectors per `PutVectors`/`DeleteVectors` call, and 2,500 vectors/second inserted-plus-deleted per index. Irrelevant for a single subject, and Cedar policy 5 caps `subjectCount` at 1 anyway — but a bulk backfill of the fixture corpus must batch and respect the ceiling.
- **Cost 4 — churn.** `systemId` changes from `search-index` to `vector-index`, because the participant now indexes embeddings rather than documents and a name that hides that would be the kind of quiet drift this repo's ADR discipline exists to prevent.

## Alternatives considered

- **Keep OpenSearch Serverless.** Rejected: correct for the archetype, and the reason the repo was expensive to run. The floor is charged for existing, not for working, which is the wrong shape for a platform that is idle by design.
- **Drop the derived-index participant entirely.** Rejected: it teaches the counter-intuitive phase-3 ordering rule (derived stores before authoritative ones) and it is the only participant that demonstrates an artifact outliving its source. Removing it to save money would be cutting the lesson, not the cost.
- **Substitute an Athena-backed index over S3** (§16 Q7's straw man). Rejected: cheap, but `analytics-lake` already teaches the columnar-store lesson, so this would be two participants teaching one thing and nothing teaching the derived-store one.
- **Tier S3 Vectors behind OpenSearch**, the AWS-documented pattern. Rejected *for this repo*: it is the right production answer for a latency-sensitive workload, and it reintroduces the exact floor this ADR removes. Recorded here so the reader knows the pattern exists rather than concluding S3 Vectors is a drop-in replacement for OpenSearch in general.

## References

- ARCHITECTURE.md §4.2 (archetype 6), §5.2 (keep the identifier alive), §14.2 (cost), §16 Q7 (**now resolved**)
- Amends [ADR-017](ADR-017-real-aws-participants.md) · relieves [ADR-020](ADR-020-deployed-eval-gate.md)'s per-run cost · `infra/README.md`
