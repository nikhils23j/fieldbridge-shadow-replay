# FieldBridge interview notes

## What it is

A local customer integration simulator that repairs webhook schema drift with durable replay, shadow validation, tenant checks, canary and approved cutover.

## How it works and why

- Raw events are inserted before delivery. That makes failures observable and replayable.
- The mapping is versioned. The candidate maps v1 and v2 while the current one maps v1.
- Event IDs are unique in the effect table; entity sequence numbers prevent stale events from replacing newer values.
- Shadow replay projects the candidate from raw events without changing the active destination. It checks tenant boundaries, preservation of existing entities, dead-letter coverage and twelve representative customer queries.
- A passing 5% canary and explicit approval precede a local SQLite transaction that rebuilds destination state. Rollback reactivates the prior mapping and rebuilds from the raw log.
- A read-only LangGraph assistant can propose which declared mapping to test. The verifier and approval gate decide what is applied.

## Results and limits

The 2026-11-11 run has 120 scenarios, 120,000 logical events, 122,860 deliveries, four profiles and six fault classes. It ended with zero valid events unapplied, zero final state mismatches, zero wrong-tenant effects and 20/20 wrong-tenant candidate mappings rejected. The direct connector baseline had 3,130 final entity mismatches, 11,180 silently dropped delivery attempts, 2,860 duplicate effects and 20,000 wrong-tenant effects. Ten tests pass, including an injected failure during local promotion and a same-ID cross-tenant case. The local-model demo chose v1+v2 but incorrectly described which schema was failing. These are synthetic results; the benchmark's expected state is generated independently from the event specification. The bad candidates cover one global tenant-routing bug class. The approval gate is a Boolean, not authenticated human sign-off. Atomic rollback is only valid for the local SQLite destination.

## Two-minute STAR answer

**Situation:** In a synthetic customer workflow, a v2 webhook renamed fields while events were retried and delivered out of order. The existing v1 connector left half the new events in a visible dead-letter queue.

**Task:** I wanted a safe migration that could prove the customer's final invoice state before changing the active mapping.

**Action:** I kept a durable raw event log, added idempotent effects and sequence-aware destination updates, then built a versioned mapping. I replayed recorded events through the candidate in shadow mode, compared invariants and customer query samples, required a canary and approval, and made local promotion and rollback transactional. I also added a read-only LangGraph diagnostic path.

**Result:** Across 120 local scenarios and 120,000 logical events, every valid event had an effect and the final state matched the independently generated reference. All 20 wrong-tenant mapping candidates were rejected. This demonstrates the local design; it does not establish a production customer deployment.

## Likely follow-ups

1. **Is delivery exactly once?** The local destination effect is idempotent by event ID. A remote API would need its own idempotency key or a reconciliation protocol.
2. **What happens if a required field is missing?** The raw event stays in the dead-letter queue with a reason. I do not count it as delivered.
3. **Why keep sequence numbers?** Delivery order is not business order. The latest entity version wins even when an older event arrives last.
4. **What does shadow mode compare?** Tenant boundaries, dead-letter coverage, existing state regression and representative customer query values; final benchmark grading uses an independent generator reference.
5. **Can a bad candidate still pass?** Yes. The twelve query checks do not cover every possible bug; one global tenant-routing class was tested across twenty cases.
6. **How does rollback work?** It restores the previous mapping and rebuilds the simulated destination in one SQLite transaction. Real external side effects need compensation.
7. **What is the model's authority?** None to write. It suggests one of two declared mappings; deterministic checks and a human gate control promotion.
8. **Why not Kafka or Kubernetes?** The bounded failure mode is mapping correctness and cutover. SQLite gives a reproducible local event log on an 8 GB M2.

## Explain cold checklist

- Draw raw log → active mapping → idempotent effect → destination.
- Show why event ID and per-entity sequence solve different problems.
- Explain the 50 dead letters before the demo cutover and 100 effects after.
- Distinguish shadow verification from final benchmark grading.
- State the local transaction limit before discussing rollback.
- Open `results/evaluation.json` and reproduce one fault case.
