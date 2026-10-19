param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,48}$')]
    [string] $Label
)
$ErrorActionPreference = 'Stop'
$labRoot = $PSScriptRoot
$projectRoot = [IO.Path]::GetFullPath((Join-Path $labRoot '../..'))
$outputRoot = Join-Path $projectRoot ('docs/telephony-lab-' + $Label)
if (Test-Path -LiteralPath $outputRoot) { throw 'Output already exists; choose a new label.' }
& docker version --format '{{.Server.Version}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker engine is unavailable. Start Docker Desktop normally first.' }
New-Item -ItemType Directory -Path $outputRoot | Out-Null
$imageName = 'servicevoice-telephony-lab:local'
$containerName = 'servicevoice-telephony-' + $Label
& docker build --progress plain -t $imageName $labRoot *> (Join-Path $outputRoot 'build.log')
if ($LASTEXITCODE -ne 0) { throw 'Image build failed; inspect the preserved build.log.' }
# No ports, credentials, Docker socket, application DB, or other host mounts.
& docker create --name $containerName --network none --read-only --cap-drop ALL `
    --cap-add NET_RAW --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges `
    --pids-limit 128 --memory 512m --cpus 1 `
    --tmpfs /tmp:rw,nosuid,nodev,size=64m --tmpfs /run:rw,nosuid,nodev,size=8m `
    --mount "type=bind,source=$outputRoot,target=/out" $imageName
if ($LASTEXITCODE -ne 0) { throw 'Container creation failed.' }
$inspectionText = & docker inspect $containerName
if ($LASTEXITCODE -ne 0) { throw 'Container inspection failed.' }
$inspection = ($inspectionText | ConvertFrom-Json)[0]
$bindingCount = @($inspection.HostConfig.PortBindings.PSObject.Properties | Where-Object { $null -ne $_ }).Count
if ($inspection.HostConfig.NetworkMode -ne 'none' -or $bindingCount -ne 0 `
    -or $inspection.HostConfig.Privileged -or -not $inspection.HostConfig.ReadonlyRootfs) {
    throw 'Container isolation validation failed; it has not been started.'
}
$safeConfiguration = [ordered]@{
    image_id = $inspection.Image
    network_mode = $inspection.HostConfig.NetworkMode
    published_ports = @()
    read_only_root = $inspection.HostConfig.ReadonlyRootfs
    privileged = $inspection.HostConfig.Privileged
    cap_drop = $inspection.HostConfig.CapDrop
    cap_add = $inspection.HostConfig.CapAdd
    memory_bytes = $inspection.HostConfig.Memory
    cpu_nanocpus = $inspection.HostConfig.NanoCpus
    pid_limit = $inspection.HostConfig.PidsLimit
    host_mounts = @('this fresh evidence directory only')
}
$safeConfiguration | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $outputRoot 'container-isolation.json')
& docker start --attach $containerName
$runCode = $LASTEXITCODE
$finalState = & docker inspect $containerName --format '{{json .State}}'
$finalState | Set-Content -LiteralPath (Join-Path $outputRoot 'container-final-state.json')
if ($runCode -ne 0) { throw 'Lab failed; evidence is preserved. No automatic retry.' }
$result = Get-Content -LiteralPath (Join-Path $outputRoot 'result.json') -Raw | ConvertFrom-Json
if (-not $result.passed) { throw 'Protocol verification did not pass.' }
Write-Output ('Evidence: ' + $outputRoot)
# The stopped container and evidence remain available for inspection.
