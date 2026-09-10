"""Processes that are not the API.

A worker in here is always optional, and that is a rule rather than an
accident: every path a worker serves has an inline equivalent the API takes
when the queue is absent, so a deployment can run one process, and the test
suite — which has no broker at all — exercises the inline half on every run. A
worker that were required would make RabbitMQ a second database, and the
argument in `app/core/broker.py` against putting the ledger behind a queue
applies just as well to putting the product behind one.

What a worker does buy is where the waiting happens. `app/workers/tts_batch.py`
exists so that a batch of five hundred items is admitted to a single GPU
`RABBITMQ_PREFETCH` items at a time instead of arriving as five hundred
concurrent HTTP handlers, and so that polling a six-hour job costs a delayed
message rather than a request nobody made.
"""
