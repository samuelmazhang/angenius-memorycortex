$env:MMX_CONFIG_DIR = "~\.mmx"
$ErrorActionPreference = "Continue"

function Run-MmxQuery {
    param([string]$Label, [string]$QueryArgs)
    Write-Host "===== $Label ====="
    try {
        $output = & cmd /c "mmx $QueryArgs" 2>&1
        $code = $LASTEXITCODE
        Write-Host "EXIT: $code"
        Write-Host ($output | Select-Object -First 40 | Out-String)
        return $code
    } catch {
        Write-Host "ERROR: $_"
        return -1
    }
}

Run-MmxQuery "T1" "search query --q `"HippoRAG NeurIPS 2024 arxiv 2405.14831`" --output text"
Run-MmxQuery "T2.1" "search query --q `"pgvector 100M vectors HNSW benchmark recall latency`" --output text"
Run-MmxQuery "T2.2" "search query --q `"Qdrant 100M benchmark performance comparison pgvector`" --output text"
Run-MmxQuery "T2.3" "search query --q `"PgBouncer PgCat Odyssey TPS benchmark onidel`" --output text"
Run-MmxQuery "T2.4" "search query --q `"LOCOMO long conversation memory benchmark snap-research`" --output text"
Run-MmxQuery "T2.5" "search query --q `"Nomic Embed Text v1.5 MTEB score 62 multilingual`" --output text"
Run-MmxQuery "T2.6" "search query --q `"Anthropic Memory Tool memory_20250818 GA release date Sonnet 4.5`" --output text"
Run-MmxQuery "T2.7" "search query --q `"Anthropic Context Editing clear_tool_uses_20250919 clear_thinking_20251015`" --output text"
Run-MmxQuery "T2.8" "search query --q `"Mem0 paper 2504.19413 LOCOMO improvement fact extraction`" --output text"
Run-MmxQuery "T2.9" "search query --q `"NeurIPS 2024 HippoRAG Multi-hop QA +20% Personalized PageRank`" --output text"
Run-MmxQuery "T3" "search query --q `"HippoRAG`" --output json --quiet"