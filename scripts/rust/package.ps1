#requires -Version 7.0
param([string]$OutputDirectory = 'dist/rust')
$ErrorActionPreference = 'Stop'

$metadata = cargo metadata --locked --format-version 1 --no-deps | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Cargo metadata failed' }
$package = $metadata.packages | Where-Object name -eq 'foreverfish-rust'
if (-not $package) { throw 'Rust package metadata is missing' }
$version = $package.version
if ($env:GITHUB_REF -like 'refs/tags/rust-v*' -and $env:GITHUB_REF_NAME -ne "rust-v$version") {
    throw "Tag $($env:GITHUB_REF_NAME) does not match Cargo version $version"
}
foreach ($file in @("docs/releases/rust-v$version.md", 'docs/Rust使用说明.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'crates/foreverfish/assets/OFL-NotoSansSC.txt', 'crates/foreverfish/assets/FONT_SOURCE.md')) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Package input is missing: $file" }
}
$manifest = [xml](Get-Content crates/foreverfish/windows.manifest -Raw -Encoding utf8)
$numericVersion = ($version -split '-')[0] + '.0'
if ($manifest.assembly.assemblyIdentity.version -ne $numericVersion) {
    throw "Windows manifest version does not match Cargo version $version"
}
$executables = @('ForeverFishing-Rust.exe', 'ForeverFishing-Rust-CLI.exe')
$releaseDirectory = Join-Path $metadata.target_directory 'release'
foreach ($name in $executables) {
    $path = Join-Path $releaseDirectory $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Build the release executables first: $path" }
    $info = (Get-Item -LiteralPath $path).VersionInfo
    if ($info.FileVersion -ne $version -or $info.ProductVersion -ne $version) {
        throw "Stale executable version in ${name}: expected $version, found $($info.FileVersion)"
    }
}
$cliVersion = & (Join-Path $releaseDirectory 'ForeverFishing-Rust-CLI.exe') --version
if ($LASTEXITCODE -or $cliVersion -ne "foreverfish-rust $version") { throw 'Native CLI version verification failed' }

New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
foreach ($name in $executables) { Copy-Item -LiteralPath (Join-Path $releaseDirectory $name) $OutputDirectory }
$commit = git rev-parse HEAD
if ($LASTEXITCODE) { throw 'Cannot determine the source commit' }
$dirty = if (git status --porcelain) { 'true' } else { 'false' }
if ($LASTEXITCODE) { throw 'Cannot determine the source state' }
$compiler = rustc --version
if ($LASTEXITCODE) { throw 'Cannot determine the Rust compiler version' }
$buildInfo = "version=$version`ncommit=$commit`nsource_dirty=$dirty`ncompiler=$compiler`ntarget=x86_64-pc-windows-msvc`npackaged_utc=$([DateTime]::UtcNow.ToString('o'))`n"
$buildInfoPath = Join-Path $OutputDirectory 'BUILD_INFO.txt'
[System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($buildInfoPath), $buildInfo, [System.Text.UTF8Encoding]::new($false))
$archive = Join-Path $OutputDirectory "ForeverFishing-Rust-v$version-Windows-x64.zip"
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ('foreverfish-rust-package-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory $stage | Out-Null
    foreach ($name in $executables) { Copy-Item -LiteralPath (Join-Path $OutputDirectory $name) $stage }
    Copy-Item LICENSE, THIRD_PARTY_NOTICES.md $stage
    Copy-Item docs/Rust使用说明.md (Join-Path $stage 'Rust使用说明.md')
    Copy-Item crates/foreverfish/assets/OFL-NotoSansSC.txt $stage
    Copy-Item crates/foreverfish/assets/FONT_SOURCE.md $stage
    Copy-Item -LiteralPath $buildInfoPath $stage
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $archive -Force
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
$artifacts = $executables + @([System.IO.Path]::GetFileName($archive), 'BUILD_INFO.txt')
$checksums = $artifacts | Sort-Object | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath (Join-Path $OutputDirectory $_) -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $_"
}
Set-Content -Encoding ascii (Join-Path $OutputDirectory 'SHA256SUMS.txt') $checksums
Get-Content (Join-Path $OutputDirectory 'BUILD_INFO.txt')
Get-Content (Join-Path $OutputDirectory 'SHA256SUMS.txt')
