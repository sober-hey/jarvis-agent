# Archive → product long-term memory (explicit opt-in)

The default `packaging.absorb.absorb()` behavior is unchanged: archive evidence
stays in `absorb.db`, separate from live memory. To make completed archive
facts available to the existing product dialogue entry, explicitly opt in:

```sh
python -m packaging.absorb /absolute/synthetic-or-authorized-export \
  --data-dir /absolute/product-data --publish-memory \
  --config /absolute/product-config.json --key-file /absolute/dedicated-key
```

Use the **same product data directory** for later chat. The CLI above describes
normal configured use; the committed acceptance tests use deterministic fake
extraction and a loopback fake provider, never real credentials or models.
`--dry-run` remains a no-publication inspection, even with the flag.

Only completed batches trigger publication. Current `fact`, `preference`,
`relationship`, `habit`, and dated `event` records are reconciled into
`product_memory.MemoryStore`. Lessons, style/persona proposals, revocations
as commands, digestion text, and suspicious records are not promoted.
The source metadata retains the archive entry, original source path/reference,
SHA-256, source tier, claimed date, calibrated confidence and conflict reason.
The dialogue prompt gets a compact first citation and citation count; full
citations remain in the local store. Archive assertions are not relabeled as
direct user statements, nor made executable instructions.

Conflict alternatives remain in `absorb.db`, but only its tentative current
winner is searchable in `memory.db`. A newly preferred conflict alternative
can become current later; automatic conflict retirement is distinct from an
explicit tombstone. Explicit revocation and product-memory deletion prevent
replay resurrection. Retirement increments the memory revision, invalidating
stale product conversation history on the next turn. Logical deletion does
not physically erase archive snapshots, source exports or audit history.

Publication is a separate, atomic transaction in `memory.db`, after durable
absorption. If interrupted between the two databases, rerun the same archive
with `--publish-memory`: completed absorption does not call the model again,
and publication reconciles its stable mappings. There is no distributed
transaction spanning both stores. User corrections/deletions take priority
over archive replay; unrelated direct-user facts are not adopted by the
archive mapping.

Extraction still owns semantic normalization. The tests cannot establish the
quality of arbitrary model extraction, resolve all synonyms or prove unknown
prompt-injection patterns safe. They establish the storage, current-state,
replay and actual offline dialogue-wire contracts for explicit synthetic data.

Offline validation:

```sh
# cwd packaging; PYTHONPATH is repository root, TMPDIR is a private non-Hermes root
python -B -m unittest -v test_absorb test_product_memory test_chat_entry
python -B -m pytest -v -o addopts= -p no:cacheprovider test_absorb.py
```

`test_absorb` exposes the end-to-end unittest class as well as the preexisting
pytest function tests, so both test runners exercise the added archive path.
