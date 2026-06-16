You sent an API request and the server rejected it with a 4xx **input-validation** error (not an
authorization refusal). The request reached the right operation but one or more field values were
malformed. Your job is to **repair the offending field(s) and resend the same call** — same operation,
same object identifier(s), only the bad values corrected.

You are given, as JSON context:
- `operator_notes` — *(if present)* operator scope constraints; honour them.
- `operation` — the operation's `key`, `method`, `path`, `parameters` and `request_body` fields, each
  with type/format/`constraints`/`description`.
- `sent_request` — exactly what you sent: `path_params`, `query`, `body`.
- `server_status` / `server_response` — the rejection. Read it: it usually names the failing field
  (e.g. a length, format, or range violation).
- `generators` — named value generators, for inputs you cannot compose yourself.

Return **one** `PlannedCall` in `calls` that re-issues this operation with the problem fixed:
- Keep the **same `op_key`**, the same `identity`, and the **same identifier values** (path/query/body
  ids) that were sent — do not change which object is targeted.
- Change only the field(s) the server complained about (or any other clearly-invalid field), giving a
  value that satisfies the field's `format`/`constraints` and the server's message. **Prefer a
  `concrete` literal you compose** (a realistic zip/email/phone/date/enum value); use a `generator`
  only if you cannot.

Keep `memory` minimal. Do not set `ready_to_attack` or `is_done`.

Context:
```json
{context}
```
