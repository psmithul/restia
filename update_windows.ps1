$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$StateFile = Join-Path $PSScriptRoot ".restia-update-state.json"
$ReadinessAttempts = if ($env:RESTIA_UPDATE_READINESS_ATTEMPTS) {
    [int]$env:RESTIA_UPDATE_READINESS_ATTEMPTS
} else { 90 }
$Rollback = $false
$RestoreData = $false
$SharedBackup = $null

for ($index = 0; $index -lt $args.Count; $index++) {
    switch ($args[$index]) {
        "--rollback" { $Rollback = $true }
        "--restore-data" { $RestoreData = $true }
        "--shared-backup" {
            $index++
            if ($index -ge $args.Count) { throw "--shared-backup requires a path" }
            $SharedBackup = $args[$index]
        }
        "--help" {
            Write-Host "update_windows.bat [--shared-backup PATH]"
            Write-Host "update_windows.bat --rollback [--restore-data]"
            exit 0
        }
        default { throw "Unknown option: $($args[$index])" }
    }
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    $output = & docker compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($ComposeArgs -join ' ') failed"
    }
    return $output
}

function Test-ImageId([string]$ImageId) {
    return $ImageId -match '^sha256:[0-9a-f]{64}$'
}

function Test-TargetImage([string]$Image) {
    if ([string]::IsNullOrWhiteSpace($Image) -or
        $Image.Contains("@") -or $Image.StartsWith("sha256:")) {
        return $false
    }
    return $Image -match '^[A-Za-z0-9][A-Za-z0-9._/:+-]*$'
}

function Wait-RestiaReady {
    $probe = @'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:7000/api/ready", timeout=3) as response:
    payload = json.load(response)
raise SystemExit(0 if response.status == 200 and payload.get("ready") is True else 1)
'@
    for ($attempt = 0; $attempt -lt $ReadinessAttempts; $attempt++) {
        & docker compose exec -T odysseus python -c $probe *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Start-PreviousImage([string]$PreviousImage, [string]$TargetImage) {
    try {
        & docker image tag $PreviousImage $TargetImage
        if ($LASTEXITCODE -ne 0) { return $false }
        # Keep the configured tag on the rollback so a later ordinary
        # `docker compose up` cannot jump back to the failed pulled image.
        $env:RESTIA_IMAGE = $TargetImage
        Invoke-Compose up -d --no-build odysseus | Write-Host
        return Wait-RestiaReady
    } catch {
        Write-Warning $_.Exception.Message
        return $false
    } finally {
        $env:RESTIA_IMAGE = $TargetImage
    }
}

if ($Rollback) {
    if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) {
        throw "No rollback state found at $StateFile"
    }
    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
    if (-not (Test-ImageId $state.previous_image)) {
        throw "Rollback state contains an invalid image ID"
    }
    if (-not (Test-TargetImage $state.target_image)) {
        throw "Rollback state contains an invalid target image reference"
    }

    if ($RestoreData) {
        if ($state.backup_mode -ne "local") {
            throw "Automatic data restore is only available for local snapshots"
        }
        $backupPath = Join-Path $PSScriptRoot ([string]$state.backup_file)
        $backupFull = [IO.Path]::GetFullPath($backupPath)
        $backupsRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "backups"))
        if (-not $backupFull.StartsWith($backupsRoot + [IO.Path]::DirectorySeparatorChar)) {
            throw "Rollback state contains an unsafe backup path"
        }
        if (-not (Test-Path -LiteralPath $backupFull -PathType Leaf)) {
            throw "Rollback snapshot is missing: $backupFull"
        }

        Write-Host "Stopping Restia and restoring the verified pre-update snapshot..."
        Invoke-Compose stop odysseus | Write-Host
        $env:RESTIA_IMAGE = [string]$state.previous_image
        try {
            $mount = "${backupFull}:/restore/restia-backup.tar.gz:ro"
            Invoke-Compose run --rm --no-deps -T -v $mount odysseus `
                python /app/scripts/odysseus-backup restore `
                /restore/restia-backup.tar.gz --yes | Write-Host
        } finally {
            $env:RESTIA_IMAGE = [string]$state.target_image
        }
    }

    $targetImage = [string]$state.target_image
    Write-Host "Restarting previous image $($state.previous_image)..."
    if (-not (Start-PreviousImage $state.previous_image $targetImage)) {
        throw "Rollback image did not become ready"
    }
    Write-Host "Rollback complete and /api/ready is healthy."
    exit 0
}

if ($RestoreData) { throw "--restore-data requires --rollback" }

$containerId = ((Invoke-Compose ps -q odysseus) | Out-String).Trim()
if (-not $containerId) {
    throw "Restia must be running so the updater can create a consistent snapshot"
}
$previousImage = ((& docker inspect --format '{{.Image}}' $containerId) | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-ImageId $previousImage)) {
    throw "Could not determine the exact current image for rollback"
}

$image = if ($env:RESTIA_IMAGE) { $env:RESTIA_IMAGE } else {
    "ghcr.io/psmithul/restia:latest"
}
if (-not (Test-TargetImage $image)) {
    throw "RESTIA_IMAGE must be a mutable Docker tag so rollback remains durable"
}
$env:RESTIA_IMAGE = $image

$modeScript = 'import os; print(os.getenv("RESTIA_DATABASE_MODE") or os.getenv("ODYSSEUS_DATABASE_MODE") or "local-single")'
$databaseMode = ((Invoke-Compose exec -T odysseus python -c $modeScript) | Out-String).Trim()

if ($databaseMode -eq "shared") {
    if (-not $SharedBackup) {
        throw "Shared mode requires --shared-backup PATH covering PostgreSQL and RESTIA_BLOB_ROOT"
    }
    $sharedFull = [IO.Path]::GetFullPath($SharedBackup)
    if (-not (Test-Path -LiteralPath $sharedFull -PathType Leaf) -or
        (Get-Item -LiteralPath $sharedFull).Length -le 0) {
        throw "Shared backup proof must be an existing non-empty file"
    }
    $backupMode = "shared-external"
    $backupFile = $sharedFull
} elseif ($databaseMode -eq "local-single") {
    $backupDirectory = Join-Path $PSScriptRoot "backups"
    New-Item -ItemType Directory -Force -Path $backupDirectory | Out-Null
    $backupName = "restia-pre-update-$([DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss'))-$PID.tar.gz"
    $containerBackup = "/tmp/$backupName"
    $backupFull = Join-Path $backupDirectory $backupName
    Write-Host "Creating and verifying the pre-update snapshot..."
    Invoke-Compose exec -T odysseus python /app/scripts/odysseus-backup `
        snapshot --out $containerBackup | Write-Host
    Invoke-Compose exec -T odysseus python /app/scripts/odysseus-backup `
        verify $containerBackup | Write-Host
    Invoke-Compose cp "odysseus:$containerBackup" $backupFull | Write-Host
    Invoke-Compose exec -T odysseus rm -f $containerBackup | Out-Null
    if (-not (Test-Path -LiteralPath $backupFull -PathType Leaf) -or
        (Get-Item -LiteralPath $backupFull).Length -le 0) {
        throw "The pre-update snapshot copy is missing or empty"
    }
    # Validate the host copy with the exact prior image so Docker-only installs
    # do not depend on a host Python environment.
    $env:RESTIA_IMAGE = $previousImage
    try {
        $mount = "${backupFull}:/restore/restia-backup.tar.gz:ro"
        Invoke-Compose run --rm --no-deps -T --entrypoint python -v $mount `
            odysseus /app/scripts/odysseus-backup verify `
            /restore/restia-backup.tar.gz | Write-Host
    } finally {
        $env:RESTIA_IMAGE = $image
    }
    $backupMode = "local"
    $backupFile = "backups/$backupName"
} else {
    throw "Unsupported database mode: $databaseMode"
}

$stateJson = @{
    previous_image = $previousImage
    backup_mode = $backupMode
    backup_file = $backupFile
    target_image = $image
    updated_at = [DateTime]::UtcNow.ToString("o")
} | ConvertTo-Json
$pendingState = "$StateFile.tmp.$PID"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText(
    $pendingState,
    $stateJson,
    $utf8NoBom
)
Move-Item -LiteralPath $pendingState -Destination $StateFile -Force

Write-Host "Pulling $image..."
Invoke-Compose pull odysseus | Write-Host
Write-Host "Restarting Restia while preserving data, logs, and blobs..."
try {
    Invoke-Compose up -d --no-build odysseus | Write-Host
    if (-not (Wait-RestiaReady)) { throw "updated container did not become ready" }
} catch {
    Write-Warning "Update failed; restoring the exact previous image."
    if (Start-PreviousImage $previousImage $image) {
        Write-Warning "Previous image is healthy again. Data was not automatically overwritten."
    } else {
        Write-Warning "Automatic image rollback failed. Snapshot: $backupFile"
    }
    throw
}

$versionScript = @'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:7000/api/version", timeout=3) as response:
    print(json.load(response).get("version") or "unknown")
'@
$version = ((Invoke-Compose exec -T odysseus python -c $versionScript) | Out-String).Trim()
Write-Host "Restia $version is ready. Snapshot: $backupFile"
Write-Host "Rollback remains available with: update_windows.bat --rollback"
