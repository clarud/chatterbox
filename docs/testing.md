# Test evidence and remaining live acceptance

Validated on 2026-11-11 with Windows 11, MSYS UCRT Python 3.12.7 and Tk 8.6.13. The user reported a successful short live OpenRouter transcription before the previous UI redesign. This release's agent-run checks use synthetic PCM/API results and isolated temporary settings/history; no real speech or paid model generation was tested.

## Automated regression

The final source suite completed with **317 passing tests** in 27.680 seconds after the layout and download-state corrections. The extracted release package is checked with the same suite. Coverage includes Win32 ABI/hotkeys, bounded audio/ring-buffer/RMS/lifecycle, DPAPI key isolation, settings migration/themes/modes, exact OpenRouter/OpenAI request contracts, auth/quota/timeout/network/JSON errors, redirect refusal, Raw bypass, conservative Polished output validation, raw recovery, separate stage timing, cancellation, clipboard rollback/ownership/sequence races, possibly-sent receipts, caret/menu/native-focus refusal, tray lifecycle, SQLite sessions/filtering/exports, optional local-provider loading/downloading, and integrated controller/session routing.

SQLite tests use 8,000 synthetic rows with 6,000 in one session. In the final source-run benchmark, first-200 chronological queries had a 4.423 ms median and 5.652 ms maximum; literal search had a 23.588 ms median and 25.912 ms maximum; combined date/application/search had a 10.260 ms median and 17.432 ms maximum. These are temporary-database observations, not performance guarantees or API latency. Search is SQLite LIKE, ASCII-case-insensitive and literal for non-ASCII casing; no language-specific Unicode case folding or FTS index is claimed.

Cloud/LLM latency has **not been measured live for this release**. The application now records transcription/refinement/insertion timings separately for your actual calls. Raw has no refinement request. Local first-call timing includes model loading. Insertion includes native checks plus a conservative 0.8-second clipboard consumption wait.

## Passed native Windows tests

Both commands passed against a dedicated external scratch process:

```powershell
python tests/native_ui_smoke.py --mode raw --trigger hotkey
python tests/native_ui_smoke.py --mode polished --trigger hotkey
```

They exercise actual global Ctrl+Shift+F12 registration/delivery, the native floating window, focus identity, cross-process Ctrl+V, production clipboard ownership/restoration, theme switching and success auto-hide. The waveform passes synthetic PCM-derived silence → signal → silence checks. Raw never calls the fake refiner; Polished calls it. The overlay never changes the original foreground input during the watch interval. Microphone and network use are explicitly false.

The optional `--trigger button` fixture was **not qualified for this release**: an ambiguous cached foreground during artificial main-window handoff caused a safe Record refusal. Production checks were not weakened to make the fixture pass. Controller tests cover Record/Stop guards; manual Record-button acceptance remains required. The hotkey path is the preferred background route.

Earlier harness attempts exposed scratch-window activation/occlusion and Windows temporary-SQLite cleanup issues. The final native fixture raises only its owned scratch windows before guarded clicks, does not seed the clipboard, and closes SQLite before temporary-directory cleanup. One failed early attempt printed successful functional checks but exited with cleanup failure; subsequent complete runs passed. Some synthetic temporary directories from earlier failed history tests may remain because a narrowly scoped cleanup attempt was blocked. No real history/audio was used.

Actual owned-window previews were inspected for light/dark workspace, Quick Dictation, scrollable Settings and pill states. Record/Copy clipping and icon overlap were fixed. Previews use illustrative text/timings/RMS, not user transcripts or measured live API performance.

## Endpoint/environment evidence

Official OpenRouter documentation and credential-free HTTPS catalogs verified dedicated multipart WAV `/audio/transcriptions`, `openai/gpt-transcribe` with audio-to-transcription capability, and text refinement through `/chat/completions` with `openai/gpt-5.6-terra`. The current authenticated `/key` endpoint returned the expected unauthenticated 401 over verified TLS. Catalog/auth checks do not prove credits, generation access or voice accuracy. See [API evaluation](openrouter.md).

Native diagnostics found a microphone and supported mono 16-bit rates; queries do not open capture or prove usable speech. The MSYS default CA store was empty; the existing distribution CA-bundle fallback reached providers with hostname/certificate verification intact. No trust settings were changed.

Optional `faster_whisper`, `ctranslate2` and `huggingface_hub` were absent. Fifty local-provider tests use mocked packages/models/downloads; integration tests confirm Local Raw does not construct a cloud adapter or read its key. **Real offline inference/downloads were not run.** Use [offline provisioning](offline.md) on official Windows x64 CPython.

## Live acceptance required

Exit the old running app and restart this release. Use non-sensitive scratch content, not passwords, executable terminal commands, real messages or financial/legal forms.

1. In Settings confirm your provider/models/key/shortcut. Check API. Enable Windows desktop microphone permission and the intended default input.
2. In blank Notepad, use the hotkey. Stay silent for 2 seconds, speak normally for 5–10 seconds, then stay silent again. Confirm the real waveform is flat/near-flat during silence and responds to speech. Stop and keep the caret unchanged; verify correct text, focus, no Enter and auto-hide.
3. Repeat Raw/Polished, comparing technical terms, numbers and negations. Record displayed transcription/refinement timings. Copy a harmless clipboard sentinel beforehand and verify restoration afterward; also try a deliberate concurrent clipboard change and confirm it is not overwritten.
4. Open `tests/browser_input.html` manually in Chrome; test input A and the textarea without switching fields during processing. Repeat in a new unsaved VS Code text buffer. The connected browser/tool was unavailable for interactive verification, so Chrome/browser acceptance was not run by the agent.
5. Hide/minimize the workspace and repeat. Test tray mode switching/open/settings/Exit. Test Record/Stop from the workspace after focusing the intended external field. If ambiguous, use the hotkey or Copy; do not override checks.
6. Create a session, dictate several entries, restart, and confirm active selection/history. Search/date/app-filter, review Raw/Polished, edit without replacing originals, copy, cancel a delete, confirm deletion, and export MD/TXT. Switch to Quick Dictation and confirm no new saved session/history row.
7. Disable application tracking and confirm metadata is absent. Leave titles off by default; enable only if comfortable saving document names. Test other monitors/scales if present.
8. For offline acceptance provision dependencies/model explicitly, select Local Whisper + Raw, disconnect network and dictate into a scratch editor. Measure cold/warm CPU latency. Polished needs internet; deliberately fail refinement and confirm raw recovery.

Still unverified: real microphone waveform/capture through this release, live STT/editor accuracy and latency, real Notepad/Chrome/VS Code insertion, mixed-monitor hardware/scales, exhaustive clipboard formats/slow consumers, all opaque DOM/document-tab changes, elevated/protected applications, actual offline CPU inference and installation. Native metadata does not inspect application contents or verify delivered text; an accepted Ctrl+V is an insertion report, not a delivery guarantee.

A previous version 0.3 test-harness incident may have cleared a seeded clipboard after failed focus/cleanup; the user was advised to re-copy needed content. Current unseeded native tests use only the production paste transaction and do not repeat that seeding path.
