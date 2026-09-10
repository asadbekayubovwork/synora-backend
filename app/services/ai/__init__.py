"""The AI microservices, as seen from this backend.

Everything in here is one side of a boundary. `tts_client` and the clients that
will follow it know a base URL, a key and a JSON shape, and nothing about
money; `app/services/billing` knows holds, prices and the ledger, and nothing
about GPUs. The orchestration modules that sit on top — `tts_service`,
`tts_batch_service` — are the only place the two meet, and they are small
precisely because neither half leaks into the other.

That split is what makes the gateway model workable. The upstream `sk_live_...`
key never leaves this process, users authenticate to us instead, and the
question "what did this cost?" is answered from our own tables rather than from
a header the upstream box happens to send. The price of that is one more hop
and one more place a request can fail, which is why the failure mapping lives
in one function per client rather than at each call site.
"""
