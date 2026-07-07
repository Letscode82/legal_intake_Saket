"""Documents module — upload + extraction on the SHARED Document entity.

Uploads are UNTRUSTED input: size/type limits enforced here, extracted text
is stored as data and must be spotlighted (fenced) whenever it reaches a
model prompt. No parsing beyond plain-text decode ships in v1 — binary
formats store with extracted_text=None and agents surface the gap instead
of guessing."""
