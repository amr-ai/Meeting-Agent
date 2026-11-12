# Shared voice regression check — 2026-11-07

The user confirmed that the current website is fast and sounds good. Its model
factory, RoomOptions, browser media/app code and local instructions were checked
against the preceding archive and preserved. Both modes use the same native model,
16 kHz input/24 kHz output path and output node. Meeting sessions now also use the
working browser path's SDK interruption defaults.

Confirmed historical failure: in session 3b1871fd-30f9-40de-85a4-0eed7d1efc4b,
three generations produced audio but the gate denied publication. The logs alone
do not prove that the same gate caused the previous local website regression.
Earlier meeting follow-up and Arabic/English transcription-hint corrections remain.

Further corrections: defer Recall context updates during thinking as well as
speaking, without advancing the cursor; seed recovery history before Live starts
instead of sending runtime instructions as a model turn; do not echo assistant
replies back as metadata user messages. Recall roster/attribution and stored
conversation history remain available. Activation policy differs by mode because
meeting discussion requires explicit address; the voice engine does not differ.

Final isolated audio tests replayed exactly the same three cached named-address
fixtures through local and meeting modes. All six trials received non-silent audio.
Local input-end to received RTC audio: 2921.7, 6550.7, 2661.4 ms.
Meeting mode: 2678.0, 11019.7, 6688.9 ms.
These are small sequential samples, not evidence of equal performance or a
statistically meaningful difference. Both show variable delay without Recall or
Google Meet in the path. They do not verify acoustic playback, answer semantics,
participant metadata or interruptions in an actual meeting. No real meeting was
joined and no existing service/call was stopped. The 1–2 second target remains unmet.

70 tests passed in 28.32 seconds. Ruff, compilation, JavaScript module syntax and
pip dependency checks passed. New checks cover unchanged PCM in both modes,
revoking audio after stop, shared SDK options, and preserving Recall annotations
while updates are deferred during thinking. Raw synthetic timings are in
SHARED_VOICE_MEASUREMENTS.json; no credentials or human meeting audio are included.
