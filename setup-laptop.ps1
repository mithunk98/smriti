# Connects this Windows laptop's Claude Code to Smriti.
# Paste the whole block into PowerShell. Press Enter at the prompt to create a
# new token (first laptop / rotating), or paste your existing token (other laptops).
& {
  $url = "https://smriti-k3zk.onrender.com/mcp"

  $t = Read-Host "Paste your Smriti token, or just press Enter to create a new one"
  if (-not $t) {
    $b = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    $t = [Convert]::ToBase64String($b)
    Set-Clipboard -Value $t
    Write-Host ""
    Write-Host "NEW TOKEN CREATED and copied to your clipboard (not shown, for safety)." -ForegroundColor Yellow
    Write-Host "1. Paste it into your private notes now (Ctrl+V)." -ForegroundColor Yellow
    Write-Host "2. Paste it into Render > smriti > Environment > SMRITI_TOKEN and save." -ForegroundColor Yellow
  }

  # Replace any earlier (possibly broken) smriti entry. Output is hidden because it echoes the token.
  claude mcp remove smriti --scope user 2>$null | Out-Null
  claude mcp add --transport http --scope user smriti $url --header "Authorization: Bearer $t" 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Host "Could not add smriti. Is Claude Code installed ('claude --version')?" -ForegroundColor Red; return }
  Write-Host "Smriti added to Claude Code." -ForegroundColor Green

  $dir = Join-Path $HOME ".claude"
  New-Item -ItemType Directory -Force $dir | Out-Null
  $md = Join-Path $dir "CLAUDE.md"
  if ((Test-Path $md) -and (Select-String -Path $md -Pattern "Smriti" -Quiet)) {
    Write-Host "CLAUDE.md already has Smriti instructions." -ForegroundColor Green
  } else {
    $text = @'

## Smriti (my personal memory)
- Before answering questions about my life, plans, decisions or preferences, call `recall`.
- When I share a decision, deadline, goal or important fact, save it with `remember` (add short tags).
- When I say "weekly review", call `recent` with days=7 and summarize what I did vs. what I planned.
'@
    [IO.File]::AppendAllText($md, $text)
    Write-Host "Smriti instructions added to $md" -ForegroundColor Green
  }

  if ((Test-Path $md) -and (Select-String -Path $md -Pattern "list_tasks" -Quiet)) {
    Write-Host "CLAUDE.md already has Smriti task instructions." -ForegroundColor Green
  } else {
    $text = @'

## Smriti tasks
- When I mention something I have to do, add it with `add_task` (convert dates like "next Friday" to YYYY-MM-DD).
- When I ask what to do, plan my day or week, or start a work session, call `list_tasks` and point out anything overdue or due soon.
- When I say I finished something, mark it with `complete_task`.
'@
    [IO.File]::AppendAllText($md, $text)
    Write-Host "Smriti task instructions added to $md" -ForegroundColor Green
  }

  Write-Host ""
  Write-Host "Done. Check the connection with: claude mcp list" -ForegroundColor Cyan
}
