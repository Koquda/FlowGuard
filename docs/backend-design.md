# Backend design

The payment owns an immutable request fingerprint and a state from `pending`, `processing`, `retrying`, `succeeded`, `declined`, or `dead_letter`. A SQLite transaction records the payment and makes it visible to workers together. Each worker claims one payment with a lease token. Only that token may complete the attempt. An expired lease can be claimed again.

The processor owns a separate durable charge ledger. Every attempt sends the payment ID as the processor idempotency key. A lost response therefore leaves a recoverable charge, and the next attempt reads the same charge. This guarantee depends on the processor retaining that key indefinitely in this demo.

An external broker was considered. It adds an outbox publisher and a second acknowledgement boundary without improving this single-host demonstration. The database queue keeps submission atomic. SQLite permits one writer at a time and is appropriate for a local demo, not a high throughput payment platform.

The API owns boundary validation and static files. `Store` owns payment transitions. `ProcessorStore` owns mock charges. `Worker` connects these boundaries. Tests exercise their observable behavior with independent database connections and a real HTTP processor.

The retry budget counts claimed attempts. A process crash after a claim leaves no result to classify, so lease recovery still makes one reconciliation request even when earlier claims exhausted the budget. A subsequent failed response moves the payment to dead letter. Successful reconciliation always wins over the retry budget. This avoids abandoning an already charged payment solely because workers crashed.

The `duplicate_charges` metric is a structural invariant of the simulated processor ledger, whose primary key permits one charge row per payment. It is not an external payment provider audit. A charge count is unknown while the processor cannot be reached.
