# Windows native audio assertion

The reported dialog names `livekit_ffi.dll`, `soxr-sys/src/fft4g_cache.h`, line 13,
and `LSX_FFT_BR == NULL`. It matches the open upstream report
[LiveKit rust-sdks #1614](https://github.com/livekit/rust-sdks/issues/1614).
That report describes a race during the first concurrent initialization of the
native resampler's FFT cache. It is a native-library assertion, not a Python
exception that application retry logic can catch.

The application now initializes relevant rate conversions and quality recipes
serially before starting worker jobs on Windows. An SDK setup callback also runs
the same idempotent, lock-protected initializer in job executors. This follows the
upstream startup-warmup mitigation; it does not modify the DLL, disable assertions,
or claim to fix the underlying native race.

Room input now requests 16 kHz mono directly, matching the installed Google
plugin's native input format, with 20 ms frames. Output stays 24 kHz mono. This
removes the plugin's otherwise necessary 24 → 16 kHz Sox conversion on the normal
input path. WebRTC still performs its necessary codec-rate conversion.

Offline verification uses public RTC APIs in fresh Python processes:

```powershell
python scripts/check_audio_runtime.py --processes 10
```

The check warms each process serially, then performs 64 resampling operations
across eight threads. It needs no credentials, microphone, browser or network.
It deliberately does not run the unsafe uninitialized reproduction or hide native
assertion dialogs. Passing the check is evidence for this bounded mitigation,
not proof of one-hour stability or resolution of the separate response timeouts.

If the dialog is already open, choose Abort, stop the old launcher with Ctrl+C,
then run the updated Run-Agent.cmd. Restarting is necessary: an already running
worker cannot acquire the startup mitigation by reloading the browser page.
