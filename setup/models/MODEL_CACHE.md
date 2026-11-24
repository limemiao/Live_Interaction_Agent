# Model Cache

Project model weights are stored in the Hugging Face cache rather than in the
project directory.

| Purpose | Hugging Face repository |
|---|---|
| ASR | `Systran/faster-whisper-large-v3` |
| Chinese embeddings | `BAAI/bge-small-zh-v1.5` |
| Vocal separation | Demucs `htdemucs` |
| Singing detection | `MIT/ast-finetuned-audioset-10-10-0.4593` |

The cache entry point is `%USERPROFILE%\.cache\huggingface\hub`; it can be
relocated with the `HF_HOME` environment variable. Model weights are not
bundled with this repository.

Project code resolves cached snapshots with `local_files_only=True`; therefore
normal runtime use does not make a model download request.
