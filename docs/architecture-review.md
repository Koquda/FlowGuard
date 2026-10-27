# Architecture candidate and independent review

## Review plan

1. Ground. Greenfield project. There is no existing application to preserve.
2. Sketch. Compare two complete storage and delivery designs.
3. Agree. Select against crash safety, readability, local setup, observability, and demonstration value.
4. Implement. Backend owner implements the selected design.
5. Scrap. Revisit the design if a safety invariant requires coordination outside its owning module.

## Candidate A. Durable SQLite queue

The API records a payment and its queued work in one SQLite transaction. A unique idempotency key identifies the immutable payment request. The worker claims due work under a short database transaction, then calls the processor outside the transaction. A claim token fences later completion writes.

The processor owns a separate SQLite ledger. A unique merchant payment identifier prevents a second charge. The processor stores its result before responding. When a response disappears, the worker retries the same identifier and receives the stored result.

The queue owns delivery state, attempts, due times, and leases. The payment owns the amount, currency, request fingerprint, and final result. The processor owns the charge. The dashboard reads those facts without deciding payment outcomes.

Proposed boundaries are `PaymentStore.submit(request)`, `PaymentStore.claim(now)`, `PaymentStore.finish(claim, result)`, `PaymentStore.replay(payment_id)`, and `Processor.charge(payment_id, request)`. Each method completes its own transaction. Callers must not coordinate partial writes.

This design keeps setup small and exposes the failure cases directly. SQLite serializes writers and requires a local durable volume. Multiple processes on one host are appropriate. A shared network filesystem and independent hosts are outside the design.

## Candidate B. PostgreSQL outbox and message broker

The API records the payment and an outbox entry in one PostgreSQL transaction. A relay publishes the outbox entry to a durable broker and marks the entry delivered. Duplicate publication remains possible if the relay crashes after publish. Consumers therefore still require idempotent handling.

Workers consume messages and call the independent processor with a stable payment identifier. The processor still needs durable idempotency. Acknowledging the message after the payment update allows redelivery after a worker crash. PostgreSQL stores replay history and terminal states. The broker owns delivery scheduling or routes failed messages to a dead-letter queue.

This design can demonstrate remote workers and independent scaling. It introduces a relay, broker delivery rules, broker persistence, and a second operational failure domain. These components do not remove the need for processor idempotency.

## Comparison and synthesis

| Criterion | SQLite queue | PostgreSQL outbox and broker |
| --- | --- | --- |
| Crash safety | Atomic submission and durable claims have a small proof. The processor still needs its own ledger. | Atomic submission is clear. The relay adds duplicate publication and acknowledgement cases. |
| Readability | One store owns each invariant. Fewer moving components. | More production delivery concepts, with more contracts to learn. |
| Local reproduction | Python standard library and two database files. | Database, broker, relay, and workers need configuration. |
| Observability | Queue state and event history can come from durable rows. | Requires correlation across outbox, broker, workers, and processor. |
| Demonstration value | Failure recovery is visible and repeatable on a laptop. | Better for distributed infrastructure study and horizontal scale. |

Select candidate A for this demo. Keep the processor separate and retain real lease ownership, durable retries, and dead-letter replay from the broader delivery model. Do not claim general exactly-once delivery. The demonstrable property is one durable processor charge for each stable payment identifier, despite repeated delivery.

## Safety invariants to verify

- Payment insertion and initial queue insertion commit together.
- Concurrent requests with the same key return one payment. Different immutable payloads with that key produce a conflict.
- Currency and amount use validated values and integer minor units. Boolean and fractional amounts must fail validation.
- A worker never holds a write transaction across a network call.
- Every completion or retry update checks the claim token. A stale worker cannot overwrite a newer claim.
- The processor commits a charge before the simulated response loss. Restarting either service preserves the charge and idempotency record.
- Retries preserve the processor key and immutable payload. An ambiguous timeout cannot cause a fresh charge identifier.
- Declines are terminal. Transient and ambiguous failures retry with a bounded policy.
- An expired lease becomes claimable. Repeated crashes eventually terminate or remain explicitly visible according to the documented attempt policy.
- Replay applies only to dead-letter work and retains the original processor key. Concurrent replays create one runnable job.
- Counters and safety claims use the processor ledger or a clearly stated scope. Missing processor data must not appear as zero charges.
- HTML output escapes event and payment data. Mutation endpoints validate their origin or require an explicit local API boundary.

## Verification status

The candidate comparison is a design judgment. Independent execution and code review results follow.

## Independent execution evidence

The reviewer ran `python3 -m unittest discover -s tests -v` against the implementation. All 10 tests passed. The test set covers concurrent duplicate submission, concurrent workers, payload conflicts, lost responses, recovery after reopening the payment store, stale lease fencing, a crash after processor charge, transient failures, terminal declines, dead-letter replay, HTTP validation, overview data, and Prometheus output.

A separate reviewer script submitted the same request through 40 concurrent store calls. It observed one payment and one initial acceptance. A changed amount raised a conflict. An expired lease received a new token. Completion with the old token failed, and completion with the current token succeeded.

A separate reviewer script ran a real temporary HTTP processor. A lost response left one charge and a retrying payment. Reopening the stores and retrying completed the payment with one charge. Transient failures recovered on the third attempt. A decline remained terminal. An outage exhausted four attempts. Replay retained the original payment identity.

The store-reopen tests model restart durability. They do not replace a subprocess kill-and-restart test or a power-loss test. SQLite persistence is contingent on a retained local volume.

The first server startup encountered slow hostname lookup. The backend owner replaced the HTTP server binding lookup with a literal host bind. The independent test suite passed after that change.

Malformed currency and scenario values initially needed explicit string guards. The backend owner added those guards, and the HTTP validation test passed.

Open review points were sent to the backend owner. Invalid processor JSON needs an explicit object check before `.get`. HTTP error responses need explicit closure. The repeated-crash attempt policy needs a documented bound or explicit recovery behavior. The processor's zero duplicate count is justified by its primary key invariant, not by an independent financial reconciliation service.

## Follow-up review

The backend owner added the malformed processor response guard and explicit HTTP error closure. The crash policy now permits a reconciliation request after expired claims consume the normal budget. A failed reconciliation response moves the payment to dead letter. A successful reconciliation records the existing charge. This policy preserves recovery for a charge whose worker crashed before recording completion.

The retry delay exponent is capped before calculation. This prevents unusually large accumulated claim counts from overflowing delay arithmetic. The processor duplicate-count limitation is now explicit in `backend-design.md`.

The reviewer reran the 10-test suite after the response and cleanup fixes with `ResourceWarning` escalated. All tests passed without resource warnings. Independent review found no remaining correctness blocker in the local demo scope. Process-kill tests, UI behavior, container execution, and deployment are outside this review's independent evidence.

## Final integration review

The final suite contains 11 tests and passed with resource warnings treated as errors. The live subprocess smoke check passed 16 concurrent submissions, conflicting payload rejection, lost-response recovery, and worker restart with one processor charge. The browser review passed the same recovery flow and a 375px mobile layout without page overflow. Docker Compose configuration passed. Local container execution remains unverified because the Docker engine was unavailable. Reviewers inherited the parent model; no cross-family review is claimed.
