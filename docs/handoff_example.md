# Synthetic customer handoff: Atlas invoice events

## Customer-visible failure

The v1 mapping accepted 50 invoice events and dead-lettered 50 renamed v2 events. The durable raw log retained every delivery for repair. This is a local synthetic scenario.

## Acceptance checks

- Every valid event has one destination effect after cutover.
- Customer invoice values match the twelve selected query checks.
- No event writes outside the Atlas tenant.
- A deliberately corrupted value mapping fails verification before promotion.

## Evidence and change

Shadow replay tested the versioned v1+v2 mapping against tenant boundaries and 12 customer query checks. The corrupted mapping was rejected by 12 query mismatches. Approved cutover replayed the log; 100 logical events have one destination effect each, with no dead letters. A direct connector would have silently dropped 50 deliveries and left 28 invoice entities in the wrong final state in this same scenario.

## Local rollback plan

Keep the current mapping while shadow checks run. Reject a failing candidate. An injected mid-promotion failure rolls back the local SQLite transaction; replay the retained log after a corrected mapping passes verification.

## Residual risk

The destination is a local SQLite transaction. An external API would need staged writes or compensation for true rollback. The customer fixtures are synthetic.

## Reproduce

python -m fieldbridge demo
