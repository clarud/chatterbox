# Development notes

Environment checked on 2026-11-11: Windows 11 build 26200; Python 3.12.7 at `C:\msys64\ucrt64\bin\python.exe`; Tk 8.6.13 starts successfully. No pip, sounddevice, keyring, or provider credential variable was found. The supported Manus Node catalog command failed to start its runtime; no built-in credentials were accessed or exported.

Implementation: dependency-free Python/Tk desktop controller; ctypes Win32 RegisterHotKey, waveIn audio capture, foreground-window restoration, clipboard snapshot/paste/conditional restoration, and user-scoped DPAPI credential encryption. Direct HTTPS API requests use Python's standard library. No microphone capture is performed by diagnostics or tests without an explicit recording action.

Verified sources:

- https://developers.openai.com/api/docs/guides/speech-to-text — multipart `/v1/audio/transcriptions`, WAV input, 25 MB limit, `gpt-transcribe` is the current recommendation. Older Whisper/GPT-4o transcription models are deprecated with shutdown stated as February 26, 2027.
- https://developers.openai.com/api/docs/models/gpt-transcribe — current audio transcription model.
- https://developers.openai.com/api/docs/models/gpt-4.1-mini and https://developers.openai.com/api/docs/models/gpt-5-mini — both reported deprecated; not selected as new application defaults.
- https://developers.openai.com/api/docs/models/gpt-5.6-terra — current mini-tier replacement, supports Responses and `reasoning.effort: none`; selected for refinement. Provider model access still needs the user's key.
- https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveinopen — native waveform input API, WAVE_MAPPER and WAVE_FORMAT_QUERY.
- https://learn.microsoft.com/en-us/windows/win32/api/ole2/nf-ole2-oleduplicatedata — format-sensitive clipboard duplication; implementation will snapshot supported formats and refuse destructive handling of unsupported ones.

Live provider access and microphone capture still require interactive verification. Automated tests must not be represented as successful live transcription.

The MSYS UCRT Python build lacks Windows certificate enumeration and loaded zero CA roots by default. Its existing sibling bundle at `C:\msys64\usr\ssl\cert.pem` loads 146 trusted CA certificates. Loading that bundle resolved certificate verification; an unauthenticated HTTPS call to OpenAI's model endpoint returned the expected HTTP 401. `tls.py` uses this narrow distribution-relative fallback only when the default store is empty and no `SSL_CERT_FILE` override is present. No certificates were installed, no verification was disabled, and no credential was sent in this connectivity check. Trust guidance: https://curl.se/docs/sslcerts.html.

The first native smoke run detected that Windows commits a clipboard sequence increment on CloseClipboard. The sequence baseline was moved after close, and the native smoke test then passed, including focus restoration and byte-exact Unicode-text/HTML clipboard preservation.

Known owner-bound registered OLE formats, including `Ole Private Data` and `DataObject`, are refused before clipboard mutation; blindly copying opaque owner pointers would not safely preserve the source object. This is deliberately conservative. Other undocumented registered formats cannot all be proven independent of their original owner.

## Version 0.2 — OpenRouter support

On 2026-11-11 the official OpenRouter STT guide confirmed `POST https://openrouter.ai/api/v1/audio/transcriptions` accepts OpenAI-compatible multipart WAV uploads and JSON/base64 audio. The existing multipart builder is reused. OpenRouter refinement uses text-only Chat Completions; direct OpenAI continues to use Responses. This avoids asking a conversational audio model to produce a literal transcript.

The public OpenRouter catalogs were fetched over verified HTTPS without credentials: 31 transcription models and 469 ordinary models at the time of the check. The selected `openai/gpt-transcribe` and `openai/gpt-5.6-terra` IDs and required modalities were present. STT discovery uses `/models?output_modalities=transcription`, not the ordinary catalog or generic audio-input support.

The current API reference documents key authentication at `GET /api/v1/key`; its credential-free local probe returned HTTP 401. The application uses that authenticated endpoint before catalog checks, because a public catalog response alone does not prove a valid key.

Fresh state selects OpenRouter. Existing settings lacking a provider field, or a legacy OpenAI key without settings, remain OpenAI without rewriting/moving the old key. DPAPI files are isolated by provider: `openrouter-api-key.dpapi` and the original OpenAI `api-key.dpapi`. The application never reads another provider's key as a fallback. Its API destinations remain fixed HTTPS allowlisted endpoints, and redirect credential forwarding remains blocked.

No provider key was supplied and no paid API generation or real microphone recording was performed. Actual model access, credits, speech quality, and latency remain interactive acceptance checks. See [OpenRouter evaluation](openrouter.md) and [testing](testing.md).

## Version 0.3 — minimalist desktop UI

The user confirmed a successful short live transcription before this redesign. The Python/Tk foundation, native audio capture, OpenRouter/OpenAI adapters, and production clipboard transaction remain unchanged.

`ui.py` now provides a quiet main screen, separate scrollable Settings view, neutral light/dark palettes, vector control icons, rounded controls, hover descriptions, and state-dependent Record/Stop/Processing/Cancel behavior. Repeated application branding/technical subtitle are removed. Standard Windows decorations remain; the title shows state and receives Microsoft's documented best-effort DWM appearance hint.

A persistent non-secret `theme` field defaults to light for pre-theme settings. Theme changes preserve provider/models/shortcut/DPAPI files. Record can now start through an explicit button callback: native external focus metadata is cached with a 30-second freshness bound, restored and verified before microphone capture, and locked for the session. No passive microphone/key/input-text capture is introduced. The global shortcut remains the direct target-capture route.

94 automated tests passed. The final native cross-process UI test passed restore-before-Record, original-target Stop, actual Ctrl+V/clipboard-state recovery, and theme switching with synthetic recorder/API components. The first version of that test had a clipboard-cleanup bug after a foreground-activation failure; it may have cleared the user's clipboard, was disclosed, and was corrected by removing all standalone clipboard seeding/restoration from the UI harness. Production clipboard code was not changed. Details and remaining checks are in [testing](testing.md) and [interface design](interface.md).
