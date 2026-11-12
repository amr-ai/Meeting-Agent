# Per-meeting speech files and additional context

Each session has two UTF-8 files under `data/transcripts/` (beside the configured
SQLite database): `<session-id>.txt` for reading and `<session-id>.jsonl` for
structured retrieval. Historical sessions are backfilled on API startup; existing
stored data can also be exported with `python -m scripts.export_transcripts`.

Files include all available final Recall utterances and the native live transcript
and assistant speech stored by the existing conversation pipeline. Each record
has a speaker, participant ID when available, source, role, text, attribution state
and timestamps. Interrupted assistant replies are marked. Control messages such
as listening state and latency diagnostics are excluded.

Speaker identity comes from verified Recall data. Native ASR without an attributed
participant stays `Unknown`/pending attribution; a sole attendee or last active
speaker is not used to invent an identity. The live ASR hypothesis and named Recall
transcript can describe the same audio differently; both sources are labeled.
Recall's duplicate mirror in the conversation table is not exported a second time.
These files are text transcripts, not a recording of raw audio or a guarantee of
perfect recognition. Participant-name updates refresh the snapshot; authoritative
post-call reconciliation replaces superseded Recall segmentation.

UTC observation times and Recall-relative speech times are labeled separately.
Meeting creation is not displayed as a fabricated speech timestamp. A single
perfect cross-provider time alignment is not claimed.

SQLite remains the durable source. The API's background writer batches dirty
sessions about once per second and writes atomic file snapshots in an OS thread.
There is no filesystem write or new model call on the voice frame path. Failures
retry from SQLite, and pending snapshots flush at shutdown. Deleting/purging a
meeting also removes its files; a queued writer cannot recreate deleted archives.

The existing meeting_context path now retrieves bounded relevant older speech
from the file, excluding records already selected from SQLite. This can recover
older speech by speaker name as well as text. It complements existing memory;
the entire file is not re-injected on every voice turn. File contents are labeled
reference data, not instructions, and stay inside the existing context budget.
The files are not mounted on the public static server.

Protocol 10 requires restarting old CMD services after any active call ends.
The one-off backfill in this change exported 15 existing sessions; continuous
updates and new file-based retrieval start when the updated service is launched.

Validation: full suite 202 passed in 65.00 seconds; after adding cancellation-safe
shutdown handling, all 8 focused archive tests passed, and Ruff passed. Tests cover
UTF-8 text, identity provenance, late names, final reconciliation, restart backfill,
additional context retrieval, per-session isolation, bounded context, failure retry,
concurrent new speech, path validation, deletion and cancellation/shutdown flushing.
No media pipeline, voice model, VAD or response gating was changed for this feature.
