# Synthetic customer handoff: Atlas invoice events

## Customer-visible failure

The v1 connector dead-lettered 50 v2 webhook events after a field rename. This is a local synthetic scenario.

## Evidence and change

The durable raw log retained the payloads. Shadow replay tested the versioned v1+v2 mapping against tenant boundaries and twelve customer query checks. A deliberately corrupted mapping was rejected (customer query mismatch: atlas/E00, customer query mismatch: atlas/E03, customer query mismatch: atlas/E05, customer query mismatch: atlas/E07, customer query mismatch: atlas/E09, customer query mismatch: atlas/E11, customer query mismatch: atlas/E13, customer query mismatch: atlas/E17, customer query mismatch: atlas/E19, customer query mismatch: atlas/E21, customer query mismatch: atlas/E23, customer query mismatch: atlas/E26). Approved cutover replayed the log; 100 logical events have one destination effect each.

## Residual risk

The destination is a local SQLite transaction. An external API would need staged writes or compensation for true rollback. The customer fixtures are synthetic.

## Reproduce

python -m fieldbridge demo
