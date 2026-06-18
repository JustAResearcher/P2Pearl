# Build pearld + prlctl natively on Windows (no WSL).
#
# Validated recipe: the Pearl node is Go + CGO linking two native libs — the zk-pow
# Rust FFI (built for the x86_64-pc-windows-gnu target; upstream's cgo directives
# already expect exactly that path) and libxmss (C, compiled with mingw gcc).
#
# Prereqs:
#   * Go 1.26+                      (winget install GoLang.Go)
#   * Rust via rustup               (https://rustup.rs)
#   * mingw-w64 (gcc/g++/ar/dlltool) e.g. https://winlibs.com — pass -MingwBin if
#     it is not at C:\mingw64\bin
#
# Usage:
#   git clone https://github.com/pearl-research-labs/pearl
#   powershell -ExecutionPolicy Bypass -File P2Pearl\tools\build_pearld_windows.ps1 -PearlDir pearl
#
# Outputs pearl\bin\pearld.exe and pearl\bin\prlctl.exe.
param(
    [string]$PearlDir = "pearl",
    [string]$MingwBin = "C:\mingw64\bin"
)
# NOT 'Stop': cargo/rustup print progress to stderr, which PowerShell 5.1 would
# promote to terminating errors. Failures are caught via explicit exit-code checks.
$ErrorActionPreference = "Continue"
$PearlDir = (Resolve-Path $PearlDir).Path

foreach ($tool in "gcc.exe", "g++.exe", "ar.exe", "dlltool.exe") {
    if (-not (Test-Path (Join-Path $MingwBin $tool))) {
        throw "mingw-w64 tool '$tool' not found in $MingwBin (pass -MingwBin <dir>)"
    }
}
go version | Out-Null
rustup --version | Out-Null
$env:Path = "$MingwBin;$env:Path"      # cargo (gnu host) + cgo need dlltool/gcc here

Write-Host "== rust target x86_64-pc-windows-gnu =="
rustup target add x86_64-pc-windows-gnu

Write-Host "== zk-pow verifier circuit cache =="
Push-Location (Join-Path $PearlDir "zk-pow")
if (-not (Test-Path "src\circuit\v2_cache.bin") -or -not (Test-Path "src\v1\v1_cache.bin")) {
    cargo run --release --no-default-features --bin build_cache src/circuit/v2_cache.bin src/v1/v1_cache.bin
    if ($LASTEXITCODE -ne 0) { throw "circuit cache build failed" }
}
Pop-Location

Write-Host "== zk_pow_ffi staticlib (windows-gnu) =="
Push-Location (Join-Path $PearlDir "zk-pow\bindings\go")
$ffiOut = cmd /c "cargo build --release --target x86_64-pc-windows-gnu 2>&1"
$ffiOut | Write-Host
if ($LASTEXITCODE -ne 0) {
    # Older stable rustc: blake3's constant_time_eq may demand a newer compiler.
    # Pin the newest version your rustc supports and retry once.
    if ("$ffiOut" -match "constant_time_eq@\S+ requires rustc") {
        Write-Host "   rustc too old for constant_time_eq - pinning 0.4.2 and retrying"
        cmd /c "cargo update constant_time_eq --precise 0.4.2 2>&1" | Write-Host
        cmd /c "cargo build --release --target x86_64-pc-windows-gnu 2>&1" | Write-Host
        if ($LASTEXITCODE -ne 0) { throw "zk_pow_ffi build failed after pin" }
    } else { throw "zk_pow_ffi build failed" }
}
Pop-Location

Write-Host "== libxmss.a (mingw) =="
Push-Location (Join-Path $PearlDir "xmss")
$tmp = Join-Path $env:TEMP "xmss_build"
New-Item -ItemType Directory -Force $tmp | Out-Null
foreach ($s in "params", "hash", "fips202", "hash_address", "wots", "xmss_core", "xmss_commons", "utils") {
    & "$MingwBin\gcc.exe" -Wall -g -O3 -Wextra -Wpedantic -c -o "$tmp\$s.o" "external\$s.c"
    if ($LASTEXITCODE -ne 0) { throw "gcc failed on $s.c" }
}
& "$MingwBin\g++.exe" -Wall -g -O3 -Wextra -std=c++11 -c -o "$tmp\xmss.o" "src\xmss.cpp"
if ($LASTEXITCODE -ne 0) { throw "g++ failed on xmss.cpp" }
& "$MingwBin\ar.exe" rcs libxmss.a (Get-ChildItem "$tmp\*.o").FullName
if ($LASTEXITCODE -ne 0) { throw "ar failed" }
Pop-Location

Write-Host "== pearld + prlctl (go build, CGO) =="
Push-Location $PearlDir
$env:CGO_ENABLED = "1"
$env:CGO_LDFLAGS_ALLOW = ".*zk_pow_ffi.*"
$env:CC = "$MingwBin\gcc.exe"
$env:CXX = "$MingwBin\g++.exe"
New-Item -ItemType Directory -Force bin | Out-Null
go build -tags xmss,zkpow -o bin\pearld.exe ./node
if ($LASTEXITCODE -ne 0) { throw "pearld build failed" }
go build -tags xmss,zkpow -o bin\prlctl.exe ./node/cmd/prlctl
if ($LASTEXITCODE -ne 0) { throw "prlctl build failed" }
Pop-Location

Write-Host ""
Write-Host "Done:"
Get-Item (Join-Path $PearlDir "bin\pearld.exe"), (Join-Path $PearlDir "bin\prlctl.exe") |
    ForEach-Object { "  {0}  ({1:N1} MB)" -f $_.FullName, ($_.Length / 1MB) }
Write-Host ""
Write-Host "Run it (mainnet):  bin\pearld.exe --notls --rpcuser=u --rpcpass=p"
Write-Host "P2Pearl then connects to http://127.0.0.1:44107"
