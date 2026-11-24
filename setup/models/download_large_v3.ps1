# Download faster-whisper-large-v3 into the standard Hugging Face cache.
$python = if (Test-Path -LiteralPath 'python') {
    'python'
} else {
    'python'
}

& $python -c "from huggingface_hub import snapshot_download; print(snapshot_download('Systran/faster-whisper-large-v3'))"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
