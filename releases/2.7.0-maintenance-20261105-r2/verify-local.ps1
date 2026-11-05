$ErrorActionPreference = 'Stop'
$artifactDir = $PSScriptRoot
$candidate = Get-Content -LiteralPath (Join-Path $artifactDir 'release-candidate.json') -Raw | ConvertFrom-Json
if (-not $candidate.publish_ready) { throw 'The candidate has not passed its release checks.' }
foreach ($role in @('web', 'core')) {
    $expected = $candidate.images.$role
    $output = & docker image inspect $expected.tag
    if ($LASTEXITCODE -ne 0) { throw "Missing local image: $role" }
    $actual = @($output | ConvertFrom-Json)[0]
    if ($actual.Id -ne $expected.local_image_id) { throw "Local image identity changed: $role" }
    if ($actual.Os -ne 'linux' -or $actual.Architecture -ne 'amd64') { throw "Wrong platform: $role" }
    if ($actual.Config.Labels.'org.opencontainers.image.version' -ne '2.7.0') { throw "Wrong product version: $role" }
}
foreach ($entry in @(
    @{ File = 'source-manifest.json'; Hash = $candidate.source_manifest_sha256 },
    @{ File = 'checks.json'; Hash = $candidate.checks_sha256 }
)) {
    $actualHash = (Get-FileHash -LiteralPath (Join-Path $artifactDir $entry.File) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $entry.Hash) { throw "Artifact changed: $($entry.File)" }
}
Write-Output 'Verified: two local 2.7.0 images and release evidence. No upload or deployment performed.'
