#Requires -Version 5.1
<#
.SYNOPSIS
  Manage the autoreview-bot Compose stack (base + optional overlays).

.EXAMPLE
  .\scripts\stack.ps1 up
  .\scripts\stack.ps1 up -Observability
  .\scripts\stack.ps1 up -InProcess
  .\scripts\stack.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("up", "down", "restart", "logs", "status")]
    [string]$Command,

    [switch]$Observability,
    # Default stack includes the container-runner overlay (M4). Kept for back-compat.
    [switch]$Container,
    # Opt out of the container-runner overlay (in_process reviews only).
    [switch]$InProcess,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
# OPR_STACK_ROOT is a test hook so behavioral tests can isolate .env / docker.
if (-not [string]::IsNullOrWhiteSpace($env:OPR_STACK_ROOT)) {
    $Root = (Resolve-Path -LiteralPath $env:OPR_STACK_ROOT).Path
} else {
    $Root = Resolve-Path (Join-Path $PSScriptRoot "..")
}
Set-Location $Root

if ($Container -and $InProcess) {
    Write-Host "Use either -Container (default) or -InProcess, not both."
    exit 2
}

# M4: container-runner overlay is on unless -InProcess.
$UseContainerOverlay = -not $InProcess

function Get-DotEnvValue([string]$Name) {
    $envVal = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($envVal)) {
        return $envVal.Trim()
    }
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        $trim = $line.Trim()
        if ($trim.StartsWith("#") -or -not $trim.Contains("=")) {
            continue
        }
        $parts = $trim.Split("=", 2)
        if ($parts[0].Trim() -eq $Name) {
            return $parts[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Assert-LauncherToken {
    $token = Get-DotEnvValue "LAUNCHER_TOKEN"
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Length -lt 16) {
        Write-Host @"
LAUNCHER_TOKEN is missing or shorter than 16 characters (required for stack up with the container-runner overlay; default since M4).

Generate one (do not commit it; put it in .env):

  PowerShell:
    -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
    # or:
    [Convert]::ToHexString((1..32 | ForEach-Object { Get-Random -Max 256 }))

  openssl:
    openssl rand -hex 32

Then set in .env:
  LAUNCHER_TOKEN=<paste value>

Or opt out of the overlay: .\scripts\stack.ps1 $Command -InProcess
(and set codex.runner_backend: in_process in config.yaml).
"@
        exit 1
    }
}

function Get-ComposeArgs {
    $composeArgs = @("-f", "docker-compose.yml")
    if ($Observability) {
        $composeArgs += @("-f", "docker-compose.observability.yml")
    }
    if ($UseContainerOverlay) {
        $composeArgs += @("-f", "docker-compose.container-runner.yml")
    }
    return $composeArgs
}

function Get-ReviewImage {
    $img = Get-DotEnvValue "OPR_REVIEW_IMAGE"
    if ([string]::IsNullOrWhiteSpace($img)) {
        return "opr-review:local"
    }
    return $img
}

function Ensure-ReviewImage {
    $image = Get-ReviewImage
    Write-Host "Building review image $image (Dockerfile.review)..."
    & docker build -f Dockerfile.review -t $image .
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

# Credential check + review image build only for up (ARB36-001 / ARB36-002).
# Lifecycle commands still use the overlay but do not rebuild.
if ($UseContainerOverlay -and $Command -eq "up") {
    Assert-LauncherToken
    Ensure-ReviewImage
}

$composeArgs = Get-ComposeArgs

switch ($Command) {
    "up" {
        & docker compose @composeArgs up -d --build @Rest
        exit $LASTEXITCODE
    }
    "down" {
        & docker compose @composeArgs down @Rest
        exit $LASTEXITCODE
    }
    "restart" {
        & docker compose @composeArgs restart @Rest
        exit $LASTEXITCODE
    }
    "logs" {
        & docker compose @composeArgs logs -f --tail=200 @Rest
        exit $LASTEXITCODE
    }
    "status" {
        & docker compose @composeArgs ps @Rest
        exit $LASTEXITCODE
    }
}
