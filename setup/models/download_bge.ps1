# Download the Chinese embedding model into the standard Hugging Face cache.
$python = if (Test-Path -LiteralPath 'python') {
    'python'
} else {
    'python'
}

& $python -c "from huggingface_hub import snapshot_download; print(snapshot_download('BAAI/bge-small-zh-v1.5'))"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
