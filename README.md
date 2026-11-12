# Chatterbox

A Windows desktop dictation tool: press a global shortcut, speak, press it again, and the transcribed (and optionally AI-polished) text is pasted into whatever field you were using — Notepad, a browser, VS Code, anything. It runs quietly in the system tray with a small floating recording indicator; no window needs to be focused.

Dictation can go through a cloud provider (OpenAI or OpenRouter) or run fully offline on your CPU (local Whisper for transcription, an optional local LLM for polishing). It also has an optional session workspace that keeps a searchable local history of what you've dictated, and a separate tool to transcribe existing audio/video files.

## Features

- **Global hotkey dictation** (default `Ctrl+Alt+Space`, configurable) that works from any app, with a non-intrusive floating indicator showing Recording/Transcribing/Refining/Success/Error.
- **Raw or Polished output** — Raw inserts the speech-to-text wording as-is; Polished conservatively cleans up punctuation, capitalization, grammar and filler words while preserving meaning.
- **Cloud or fully offline** — use OpenAI or OpenRouter, or run Whisper transcription and LLM polishing entirely on your own CPU with nothing sent over the network.
- **Sessions and history** — organize dictations into named sessions (or use "Quick Dictation" for nothing saved), browse/search/filter past entries, edit, export to Markdown/TXT.
- **File transcription** — transcribe an existing audio/video file and keep it in a separate, persistent "File Transcripts" list.
- **Safe insertion** — the paste never fires if the destination window/field changed, and your clipboard is restored afterward.
- **Privacy-conscious by default** — window-title collection is off by default, audio is never written to disk, and API keys are stored with Windows-user-scoped DPAPI encryption, never in plain text.

## Requirements

- Windows 10/11
- Python 3.12+ for Windows, with Tk (the standard python.org installer includes this — just make sure "tcl/tk and IDLE" stays checked during install)
- A microphone
- For cloud dictation: an API key from [OpenAI](https://platform.openai.com/api-keys) or [OpenRouter](https://openrouter.ai/keys)

No other packages are required for cloud-based dictation — the core app is pure Python standard library plus native Windows APIs (via `ctypes`). Offline/local features need a few additional packages; see below.

## Setup

Clone or download this repository, then from its folder:

```powershell
python -m chatterbox
```

or just double-click `launch.cmd`, which runs the app hidden from the console (via `pythonw`).

To start hidden in the tray right away:

```powershell
python -m chatterbox --background
```

There is one running instance per Windows user session — launching again while it's already running just brings a notice instead of starting a second copy.

### Optional: offline/local features

Local Whisper transcription and local LLM polishing need extra packages that aren't installed by default:

```powershell
python -m pip install -r requirements-offline.txt
```

This installs `faster-whisper`, `huggingface-hub`, and `llama-cpp-python`. Notes:

- `llama-cpp-python` (needed only for local LLM polishing) doesn't always have a prebuilt wheel for every Python version, in which case pip compiles it from source — this needs a C++ build toolchain (e.g. [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)) and can take several minutes. If you only want local *transcription* (Raw mode), you can skip `llama-cpp-python` and just install `faster-whisper` and `huggingface-hub`.
- Installing these packages does **not** download any model weights — that's a separate, explicit step inside the app (see below). Nothing is downloaded automatically.
- See [docs/offline.md](docs/offline.md) for more detail on how local inference works and its limitations.

## How to use

### First-time setup

1. Open the app and go to **Settings**.
2. Pick **OpenAI** or **OpenRouter** as your provider and enter your API key via **Set API key** (masked input, never typed into a file or chat).
3. Click **Check API** to confirm the key and model access work (this doesn't record audio or make a billed generation request).
4. Choose **Raw** or **Polished** as your default output mode, and set your preferred hotkey if you don't want the default.
5. *(Optional)* To go fully offline instead: set Transcription source to **Local Whisper**, pick a model size (tiny/base/small), click **Download local model**, then Apply. For local polishing too, set Refinement to **Local (LLM)**, click **Download refinement model (LLM)**, then Apply.

### Dictating

1. Click into any text field in any app (Notepad, a browser, your editor, a chat app, etc.).
2. Press the hotkey, speak, and press it again to stop. The floating indicator shows live audio levels while recording.
3. The transcribed (and optionally polished) text is pasted in automatically — `Ctrl+V` is sent to the original field, never `Enter`. Maximum recording length is 120 seconds.
4. If something goes wrong (polishing fails, the destination field changed, etc.) the app keeps the raw text so you can insert or copy it manually instead of losing it.

You can also open the app window to use **Record** there instead of the hotkey, or switch Raw/Polished from the tray icon.

### Sessions and history

- **New session** groups dictations together under a name you choose; select one from the sidebar to see its history as a scrollable thread, with search and date/application filters.
- **Quick Dictation** is for one-off text that you don't want saved anywhere — it's never written to history.
- Each entry can be reviewed as Raw, Polished, or Edited text, copied, edited, or deleted. Export a session to Markdown or plain text from its context menu.

### Transcribing a file

Click **Transcribe file…** in the sidebar to run local Whisper transcription (no refinement) against an existing audio/video file. Results are saved under the separate **File Transcripts** section in the sidebar, independent of your dictation sessions, and can be reopened or deleted later.

## Data and privacy

- Settings and keys live under `%LOCALAPPDATA%\Chatterbox`, outside this repository.
- API keys are encrypted with Windows user-scoped DPAPI (`api-key.dpapi` / `openrouter-api-key.dpapi`) — never stored in plain text, never synced between OpenAI/OpenRouter.
- Session history (`history.sqlite3`) and file transcripts (`transcripts.sqlite3`) are local, unencrypted SQLite databases — don't dictate passwords or secrets into a tracked session, and encrypt your disk if that matters to you.
- Audio is never written to disk; it stays in memory only for the duration of processing.
- Application-name tracking is on by default (which app you dictated into) but window-title collection is off by default, since titles can contain private document/page names — both are configurable in Settings.
- Cloud mode sends your recording (for transcription) and/or transcript text (for polishing) to your chosen provider; local mode sends nothing anywhere once models are downloaded.

## Testing

```powershell
python -m pytest tests/ -q
```

This runs the full automated suite (unit tests with synthetic audio/API responses, mocked peripherals, and isolated settings/history — no real microphone or network access). There are also a couple of opt-in, interactive native-Windows smoke tests that exercise the real hotkey/focus/clipboard path:

```powershell
python tests/native_ui_smoke.py --mode raw --trigger hotkey
python tests/native_ui_smoke.py --mode polished --trigger hotkey
```

Don't touch the keyboard/clipboard/focus while these run. See [docs/testing.md](docs/testing.md) for more on what's covered.

## Troubleshooting

- **Microphone errors**: enable microphone access for desktop apps in Windows Settings, and check the correct default input device is selected in Sound settings (there's no in-app device picker).
- **API errors**: 401 is a bad/missing key, 402 is OpenRouter credits/budget, 403/404 is permissions or an unavailable model, 408 suggests a shorter recording, 429 is a rate limit.
- **Local download fails or hangs**: retry — transient network drops during the ~1GB LLM / few-hundred-MB Whisper download are the most common cause. If a download gets stuck and never finishes, fully exit the app (tray → Exit) before retrying.
- **Nothing pastes after recording**: if you switched windows/fields while it was transcribing, the app deliberately refuses to paste into the wrong place — use the in-app preview to copy the text manually instead.
- The app logs uncaught errors to `%LOCALAPPDATA%\Chatterbox\error.log` (there's no console window since it runs via `pythonw`), which is the first place to check if something crashes.

## Project structure

```
chatterbox/
  app.py                   application wiring, event loop, lifecycle
  settings_controller.py   settings-page actions (keys, models, downloads, theme)
  recording_controller.py  record/transcribe/refine/insert state machine
  sessions.py               session/history actions
  pipeline.py               mode/result/cancellation/timing, no UI dependency
  api.py                    cloud provider routing (OpenAI/OpenRouter) + local composition
  local_stt.py              optional local Whisper transcription (lazy-imported)
  local_llm.py              optional local LLM refinement (lazy-imported)
  local_refine.py           rule-based (non-LLM) local refinement
  audio.py                  native waveform capture + RMS
  windows.py / clipboard.py / destination.py   native focus/paste/insertion safety
  overlay.py / tray.py      floating recording indicator, system tray
  history.py / transcripts.py   SQLite-backed session history / file transcripts
  settings.py               settings schema, validation, DPAPI key storage
  ui.py / workspace.py      Tk desktop interface
tests/                      automated suite + opt-in native smoke tests
docs/                        design rationale and historical development notes
```

See [docs/framework.md](docs/framework.md) for why Tk/native Win32 APIs were chosen over a GUI framework with more dependencies, and [docs/interface.md](docs/interface.md) / [docs/offline.md](docs/offline.md) / [docs/openrouter.md](docs/openrouter.md) for more detail on specific subsystems.

## Known limitations

- Windows only (uses native Win32 APIs directly for hotkeys, focus tracking, and clipboard handling).
- No streaming/partial transcripts — a full recording is sent/processed as one unit.
- No in-app audio input device selector; it uses whatever Windows' default input device is.
- Local inference is CPU-only; latency depends on your hardware and chosen model size.
