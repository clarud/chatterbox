# Optional local (offline) inference

Cloud (OpenAI/OpenRouter) is the default. Local Whisper transcription and local LLM refinement are opt-in backends behind the same transcription/refinement interface the cloud path uses. **Local Raw** bypasses the cloud adapter entirely — nothing is sent anywhere once the model is downloaded. **Local Polished** (rule-based) does simple regex-style cleanup with no network and no LLM. **Local (LLM)** refinement runs a small local language model on CPU for real rewriting, still with no network use.

## Install the optional packages

From the project folder, with a standard Windows Python 3.12+ install:

```powershell
python -m pip install -r requirements-offline.txt
```

This installs:

- `faster-whisper` (CPU speech-to-text; pulls in `ctranslate2` and `av` as its own dependencies)
- `huggingface-hub` (used only to download model weights on your explicit action)
- `llama-cpp-python` (CPU LLM inference, needed only for Local (LLM) refinement)

`llama-cpp-python` doesn't always have a prebuilt wheel for your exact Python version — if not, pip compiles it from source, which needs a C++ compiler (install [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) with the "Desktop development with C++" workload) and can take several minutes. If you only want local transcription, you can skip `llama-cpp-python` and just install the other two.

Installing these packages does not download or load any model weights by itself — that only happens through the explicit in-app download buttons described below.

## Download models (explicit, in-app only)

In **Settings**:

- **Transcription**: choose a local CPU model — **tiny**, **base**, or **small** (base is the default, multilingual). Click **Download local model** and confirm. This pulls from the fixed public repo `Systran/faster-whisper-{model}` on Hugging Face with no authentication token, and does not use your cloud API key. Models are stored under `%LOCALAPPDATA%\Chatterbox\models\{tiny,base,small}`.
- **Refinement**: choose **Local (LLM)** and click **Download refinement model (LLM)**. This pulls `Qwen/Qwen2.5-1.5B-Instruct-GGUF` (a ~1GB Q4-quantized file) from its public Hugging Face repo, stored under `%LOCALAPPDATA%\Chatterbox\models\llm`.

Neither download has byte-level progress — only coarse status text — and a cancelled/interrupted download leaves partial files in place for a retry rather than deleting them. If a download hangs instead of failing or completing, fully exit the app (tray → Exit, not just closing the window) before retrying, since a stuck download thread from one attempt can't recover on its own within the same running process.

Then select **Local Whisper** as the transcription source (and, if wanted, **Local (LLM)** or **Local (rule-based)** as refinement) and apply settings. Local refinement always requires Local Whisper transcription — the app enforces this and will tell you if the combination is invalid. Raw mode needs no cloud key at all once the model is downloaded.

## How local inference behaves

- The app never imports `faster_whisper`/`llama_cpp`/`huggingface_hub` at startup — only when you explicitly choose a local backend and click Check API/Apply or run a dictation.
- First transcription loads the model with `local_files_only=True`, `device="cpu"`, `compute_type="int8"`, capped CPU threads, and reuses the loaded model afterward (no reload per dictation).
- Audio stays in memory (`BytesIO`); no temporary WAV file or cloud fallback is used for Local Raw.
- Local LLM refinement loads the GGUF model via `llama-cpp-python`'s `Llama` class on first use and reuses it; inference is serialized (one request at a time).
- Both local paths are CPU-only — no GPU is used or required. Latency depends on your hardware, model size, and recording length; the first dictation after startup includes model-load time.
- Cancellation can stop the pipeline between stages but cannot interrupt a model that's already mid-load or mid-inference; shutdown waits for it to finish.

## Limitations

- Smaller Whisper models (tiny) trade accuracy for speed; there's no in-app benchmark of that tradeoff for your voice/hardware — try a few short dictations and compare.
- No streaming or partial transcripts for local inference, same as cloud mode.
- The local LLM is a small (1.5B parameter) model chosen for CPU feasibility, not maximum quality — expect it to be less capable than the cloud refinement models for unusual phrasing or unfamiliar terminology.

## References

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — the CPU speech-to-text engine used for local transcription.
- [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base) — the default multilingual local transcription model.
- [llama-cpp-python](https://github.com/abetlen/llama-cpp-python) — the CPU inference bindings used for local LLM refinement.
- [Qwen2.5-1.5B-Instruct-GGUF](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF) — the local refinement model.
