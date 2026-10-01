# Safe API error diagnostics

## Reproduced failure

Baseline: remote `main` at `de350409c323bc80a148fce4697bd731669b6d72`.
An injected, offline compatible endpoint returned `context_length_exceeded`,
`model_not_found`, `insufficient_quota`, and `rate_limit_exceeded`.
Every exception exposed only `HTTP_ERROR` and the HTTP status.

The real network transport discarded HTTP error bodies. The client rejected all
non-2xx responses before interpreting the error envelope. Capability probing
treated every 429 as rate limiting, while service and browser paths retained
only generic status codes. The optional response collector could retain a
reflected source fragment from injected HTTP errors and 200 error envelopes.

The repository uses its synchronous standard-library compatible client, with
injected transports in tests. There is no provider SDK or token-streaming model
request path. Continuation uses the same completion client.

## Resulting contract

- Read bounded error data privately and project it into fixed categories and
  fixed Chinese explanations. Never return arbitrary provider error prose.
- Prefer recognized structured error codes/types, then narrow complete-message
  patterns. Status-only 429 remains ambiguous between rate and quota; unknown
  upstream 5xx does not imply excessive input, unsupported models, or quota.
- Separate request body size, model context, single output limit, account quota,
  rate, authentication, permission, timeout, connection, invalid response, and
  local processing failure. Display the evidence basis used for classification.
- Generate a local correlation ID for every diagnosis. Preserve a separately
  labeled upstream ID only when it passes the format and reflection checks.
- Carry safe diagnoses through connection checks, main chat, continuation,
  legacy analysis, strict analysis, job failures, and local API errors.
- Rebuild the field allowlist for shareable diagnostic export. The browser also
  reconstructs fixed labels rather than trusting server-supplied error prose.
- Omit error bodies even when optional API response capture is enabled.
  Successful model response capture retains its existing, explicitly local
  behavior; that response view is distinct from the shareable safe export.
- Stop further provider calls after an API or response parsing failure. Preserve already received
  answer text and evidence references after a continuation failure. Existing
  successful-response continuation budgets are unchanged.

## Safe example and limits

For an explicit `insufficient_quota` response, the UI gives the fixed quota
explanation, suggests checking account quota/billing, and labels the HTTP status
and local correlation ID. The shareable data includes only allowlisted fields:

```json
{
  "schema_version": "safe-api-error/v1",
  "category": "quota_exhausted",
  "evidence_source": "provider_code",
  "provider_code": "insufficient_quota",
  "http_status": 429,
  "request_id": "local-0123456789abcdef0123456789abcdef",
  "request_id_source": "local"
}
```

The local ID correlates the application's saved result, safe terminal diagnostic,
and diagnostic export; it is not a provider-issued ticket. Browser-generated
IDs are explicitly labeled as browser-local and cannot identify a server request.
A missing upstream ID, token ceiling,
remaining account balance, or reset time cannot be reconstructed. Numeric
`Retry-After` is retained when supplied; it never schedules a model retry.
Unrecognized HTML, text, or provider error formats yield an explicit unknown
cause and a safe next step. The provider must supply usable evidence to identify
its underlying failure more precisely.

## Verification

Offline regressions cover the four reported causes, unknown 500/429,
HTML/non-JSON bodies, credential/source reflection, response capture, safe
export, and retention of the first answer after continuation failure.
All fixtures are synthetic and neutral. No company source or private knowledge
was accessed, no paid model was called, and no deployment was performed.

Validation: 1,461 Python tests and 99 frontend tests passed. The final request-ID
case-folding refinement also passed all 15 classifier tests. Loopback HTTP tests
used local servers and injected provider responses. Independent review and
resolved findings are recorded in the pull request.
This change improves error diagnosis; it cannot establish the actual cause of
an unobserved company gateway failure.
