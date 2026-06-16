You previously proposed relation edges from a source operation to a group of target
operations. Validation against the specification found problems. Revise your proposal so that
every edge is grounded in the context.

Fix only what the errors call out:
- A flagged source/target field does not exist — correct it to a json_pointer, parameter
  name, or location that appears verbatim in the context, or drop the edge if no real match
  exists.
- A flagged type mismatch — add or correct the `cast`, or drop the edge.
- Do NOT add new, unrelated edges. Keep the edges that were not flagged.

Every json_pointer, parameter name/location, media_type, and target_key you output must
appear verbatim in the context below.

Validation errors:
{errors}

Your previous proposal:
```json
{previous}
```

Context:
```json
{context}
```