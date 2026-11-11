# FieldBridge: shadow-replayed customer integration cutover

## TL;DR

I built a local webhook integration that keeps the original events, tests a candidate field mapping in shadow mode, and requires a passing comparison plus approval before cutover. In the 11 November 2026 synthetic evaluation, **120 runs** across four customer profiles, six fault classes and five seeds processed **120,000 logical events / 122,860 deliveries**. After faults stopped, every valid event had one recorded effect, the final state matched an independently generated reference, and **20/20 wrong-tenant mapping candidates were rejected**. A deliberately simple direct connector had 3,130 final entity mismatches, 11,180 silently dropped delivery attempts, 2,860 duplicate effects and 20,000 wrong-tenant effects across the same matrix. These are results from my local simulator, not a customer deployment.

![Fault matrix](docs/img/fault_matrix.png)

## Why I made this

A webhook endpoint can return success while a renamed field quietly drops business records. Retries and reordering make the repair harder: applying a new mapping directly can duplicate effects or corrupt the latest value. I wanted a repair path that showed the actual destination state before the change became active.

## Architecture

```mermaid
flowchart LR
    S[Four synthetic customer sources] --> H[Local HTTP ingress]
    H --> L[(SQLite raw event log)]
    L --> M[Active versioned mapping]
    M --> I[Idempotency and sequence check]
    I --> D[(Local destination state)]
    L --> R[Shadow replay with candidate mapping]
    R --> V[Contract and customer query verifier]
    V --> C[5 percent canary]
    C --> A[Human approval]
    A --> P[Atomic local promotion]
    P --> D
```

The four profiles stand for invoice amounts, inventory units, active seats and shipment progress. Each has a v1 contract and a renamed v2 contract. The raw log, destination effects and current mapping are durable in SQLite. A repeated event ID increments its delivery count but does not create another effect. For an entity, a lower sequence number cannot overwrite a newer value. A local 429 is retried within a bounded budget; malformed or unsupported events remain visible in the dead-letter queue.

```mermaid
sequenceDiagram
    participant Operator
    participant Bridge
    participant Shadow
    participant Destination
    Operator->>Bridge: Submit candidate mapping
    Bridge->>Shadow: Replay recorded events
    Shadow-->>Operator: State diff and contract checks
    Operator->>Bridge: Approve passing candidate
    Bridge->>Destination: Canary, then atomic local rebuild
    Bridge-->>Operator: Active version and handoff evidence
```

The optional [LangGraph diagnostic assistant](fieldbridge/assistant.py) reads counts and schema outcomes, proposes either keeping v1 or considering the declared v1+v2 mapping, and calls the same deterministic verifier. It cannot approve or write to the destination. The model is called once per diagnosis; event processing does not depend on it.

## Results

The reproducible workload is four profiles × six fault classes × five seeds, with 1,000 logical events per run. Faults are schema drift, missing required fields, duplicate deliveries, out-of-order delivery, transient 429s and a wrong-tenant candidate. The [saved results](results/evaluation.json) include each case, environment, command, seeds and load average.

| Measure across 120 runs | FieldBridge | Direct connector baseline |
| --- | ---: | ---: |
| Final entity state mismatches | 0 | 3,130 |
| Valid events left unapplied | 0 | Not tracked by baseline |
| Silent delivery drops | 0 | 11,180 |
| Duplicate destination effects | 0 | 2,860 |
| Wrong-tenant effects | 0 | 20,000 |
| Bad tenant mappings rejected before cutover | 20/20 | No gate |

The local HTTP [demo result](results/demo.json) starts with 50 accepted v1 events and 50 visible v2 dead letters. A bad value mapping fails twelve customer query checks. The approved v1+v2 mapping ends with 100 accepted effects and no dead letters. The handoff is in [docs/handoff_example.md](docs/handoff_example.md).

In the separate [local-model demo](results/model_demo.json), the assistant chose the useful v1+v2 candidate, but its explanation incorrectly said v1 was dead-lettered; the actual counts show v2 was. This is why the assistant's prose is not used as cutover evidence.

**What these numbers do not show:** the benchmark uses synthetic customers and fault generators I wrote. The independent expected state is generated from the event specification, but the contract checks used at cutover are also synthetic. All twenty bad mapping candidates corrupt tenant routing in the same way; 20/20 is not a general detection rate for arbitrary bugs. The destination is a local SQLite transaction, so rollback is atomic here; an external SaaS API would need staging or compensating writes. The baseline is intentionally simple and does not represent a tuned commercial connector. I do not claim production latency from these runs.

## Run it yourself

On an Apple silicon Mac with Python 3.11:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m fieldbridge demo
.venv/bin/python -m fieldbridge evaluate --events 1000 --seeds 5
.venv/bin/python scripts/make_figures.py
```

The core demo and benchmark use no paid API. To include a real local model diagnosis, run `.venv/bin/python -m fieldbridge demo --model`; it downloads the quantized [Qwen2.5 1.5B MLX model](https://huggingface.co/mlx-community/Qwen2.5-1.5B-Instruct-4bit) on first use.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests exercise a live localhost HTTP request, source identity rejection, schema drift repair, wrong-tenant rejection, duplicate and reordered deliveries, dead letters, 429 retries, approval, atomic rollback under an injected failure and the read-only diagnostic graph.

## What this isn't

It is not evidence of a real customer integration, stakeholder discovery, production deployment or exactly-once delivery to an arbitrary external API. The reversible promotion works because both the simulated destination and mapping live in one SQLite transaction. A real deployment would need a destination-specific cutover and compensation plan.

I built this with AI pair-programming help. The implementation and measurements are available here so I can explain and defend the tradeoffs.
