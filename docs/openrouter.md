# OpenRouter support and transcription choice

OpenRouter is a suitable option for this stop-to-transcribe dictation app because it now provides a dedicated speech-to-text endpoint: `POST https://openrouter.ai/api/v1/audio/transcriptions`. It accepts OpenAI-compatible multipart uploads as well as JSON/base64 audio, and returns a JSON `text` field. Reusing the existing multipart WAV upload is the smallest change: no SDK, base64 expansion, or new audio dependency is needed. [1]

Use dedicated transcription rather than an audio-capable conversational model. OpenRouter distinguishes transcription from audio input through Chat Completions/Responses: the former is intended to produce a transcript; the latter is intended for audio analysis, questions, and conversational responses. Keeping literal transcription separate from conservative text editing reduces the chance of the audio model answering or summarizing spoken content instead of transcribing it. This is an architectural judgment, not a measured accuracy guarantee. [1] [2]

The public OpenRouter catalogs were fetched from this computer over verified HTTPS on 2026-11-11. The transcription-filtered catalog listed 31 models, including `openai/gpt-transcribe` with audio input and transcription output. The text catalog listed `openai/gpt-5.6-terra` with text input/output and reasoning controls. These names preserve the first app's underlying model choices while routing both steps through the user's requested OpenRouter account. They are not claimed to be the cheapest models or benchmarked for your voice. Catalog availability is not proof that a particular key has sufficient credits or model/provider access. [3] [4]

## Why this route fits the app

One OpenRouter key can fund both transcription and refinement. The app can keep its existing two-stage pipeline, 120-second recording bound, Windows microphone capture, focus restoration, and clipboard transaction. The largest current WAV at 48 kHz mono/16-bit is about 11.52 MB, below OpenRouter's documented 25 MB multipart limit. Its approximately 60-second upstream limit is processing time, not a 60-second recording-length limit. A two-minute clip can still time out depending on model/provider/network conditions; the first live test should be only 5–10 seconds. Long-audio chunking and streaming are not included in this update. [1]

OpenRouter adds a routing intermediary. That can simplify account management and model access, but may add latency and another data-handling party. It does not automatically improve accuracy over a direct provider call. Ordinary per-request provider-pinning controls are not applied to transcription; the STT guide supports data-policy preferences such as `zdr` and `data_collection`, but those are not enabled by this app. Do not assume zero retention or a particular upstream provider. If strict provider pinning, a retention agreement, predictable very-low latency, or streaming partial transcripts becomes a requirement, a direct dedicated speech API may be the better choice. [1] [5]

## Request and validation behavior

Speech uses multipart `file` plus the provider-qualified speech model on `/audio/transcriptions`. Refinement uses text-only `/chat/completions` with the conservative editing instruction and the selected text model. It does not send the audio to the refinement model. Only a completed, nonempty assistant text result is accepted; refusal or truncated output stops insertion and leaves the raw transcription in the preview. The default text model receives `reasoning.effort: none`, supported by its current catalog entry and OpenRouter's reasoning controls. [7] [8]

The provider base URLs are fixed to `https://openrouter.ai/api/v1` and `https://api.openai.com/v1`, not an arbitrary URL entered by a user. Each provider has its own encrypted key file. Changing provider does not copy, migrate, or forward the other provider's credential. Existing OpenAI settings/key files remain compatible.

“Check API” validates the OpenRouter key through the current documented authenticated `GET /api/v1/key` endpoint before checking models. Its credential-free local HTTPS probe returned the expected 401. It checks speech against `/models?output_modalities=transcription`, not only the ordinary text catalog. A generic audio-input model is not accepted as a speech-to-text model. Neither authentication nor catalog checks record the microphone or issue paid generation calls. These checks do not prove account credits or actual generation access. [1] [6]

## What still requires your key and voice

The user reported a successful short live transcription on 2026-11-11 before the version 0.3 interface redesign. This supports the chosen route for that short test, but does not establish accuracy, latency, or compatibility for every voice, recording length, model, and destination. The redesign uses synthetic responses for testing and does not read or replace the real key. After restarting, repeat a short scratch-editor recording through the new controls; compare numbers, names, negations, filler removal, and punctuation before using important output elsewhere. Enter keys only through the app's masked dialog, not source code or chat.

## References

[1]: https://openrouter.ai/docs/guides/overview/multimodal/stt "OpenRouter speech-to-text guide"
[2]: https://openrouter.ai/docs/guides/overview/multimodal/audio "OpenRouter audio input and output guide"
[3]: https://openrouter.ai/api/v1/models?output_modalities=transcription "OpenRouter public transcription model catalog"
[4]: https://openrouter.ai/api/v1/models "OpenRouter public text model catalog"
[5]: https://openrouter.ai/blog/tutorials/transcription-on-openrouter/ "OpenRouter transcription tutorial and routing limitations"
[6]: https://openrouter.ai/docs/api/api-reference/api-keys/get-current-key "OpenRouter current API key information endpoint"
[7]: https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request "OpenRouter Chat Completions API reference"
[8]: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens "OpenRouter reasoning controls"
