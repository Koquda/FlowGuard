# FlowGuard build plan

## Workflow

- [x] Read the Principles section of poteto-mode.
- [x] Phase A: Frame.
- [x] Phase B: Design the workflow.
- [x] Phase C: Run the loop.
- [x] Phase D: Keep the audit trail.
- [x] Phase E: Verify and hand back.

## Definition of done

Concurrent requests with one key create one logical payment. Reusing the key for a different payload fails. A processor that commits a charge and loses its response still records one charge after recovery. A crashed worker's lease expires and another worker completes the payment. Retry exhaustion reaches a visible dead-letter state with safe replay. The dashboard displays persisted state. Infrastructure and monitoring run from versioned configuration. Tests and CI rerun these checks.

## Units and checkpoints

1. Create the public MIT repository before implementation.
2. Compare a database queue with an external broker. Write payment state and API contracts before implementation.
3. Build the durable queue, separate processor ledger, and behavior tests.
4. Build the dashboard against the agreed API. Apply apple-design to feedback, hierarchy, responsive layout, and accessibility.
5. Add local orchestration, monitoring, CI, and operating instructions.
6. Verify crash recovery and browser interactions. Review the actual diff and publish the result.

The highest rigor applies to duplicate charges, transaction boundaries, and lease ownership. UI styling uses browser review. Cloud deployment and real payment integration are outside this demo.

## Throughput checkpoint

Work is split across backend, UI, and independent architecture review. The coordinator owns orchestration, documentation, and integration verification. Workers write separate paths. The first integration checkpoint is the overview API contract. The next is a complete lost-response payment through the live API and worker.

## Delivery choice

Publish the greenfield implementation directly to the newly created repository. A separate pull request is skipped because there is no existing application baseline to preserve. Cloud deployment is not part of this run.
