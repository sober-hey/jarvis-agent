# JARVIS Web Interface

The product surface is a single continuous timeline. The browser does not create or display separate sessions. `GET /api/timeline` reads the merged timestamp-ordered record and returns each row's timestamp and `source_channel`; `POST /api/timeline/chat` sends to the server-owned durable timeline identity. Existing `POST /api/chat` remains available for compatibility and keeps its legacy explicit-session semantics.

The main viewport is reserved for the future 3D avatar. Text input is intentionally collapsed into a small control at the bottom and expands on click. This round does not add real-time voice capture or playback. Source-channel values are `web`, `voice`, `email`, and `imessage`; the web UI sends `web`.

Browser verification: `EVIDENCE_DIR=/absolute/new-directory node apps/web/verify_web.mjs` starts an isolated fixture and Chrome. It does not call a model.

The service still requires an explicit private config, key file or credential FD, and data root. Never put credentials in browser storage, URLs, logs, or evidence. Use `--acceptance-mode` with a disposable `/tmp/jarvis-acceptance-*` root for real-model checks.
