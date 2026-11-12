# Fleet agent for Windows: prints one JSON sample per tick on stdout.
# Windows PowerShell 5.1, no admin, nothing installed. Sources: nvidia-smi, CIM performance classes (their names are
# not localised, unlike Get-Counter paths). The server pipes this file over SSH and prepends $Interval and $MaxLife.
# The agent exits when its sshd session process is gone or after $MaxLife seconds, so a dropped SSH session never
# leaves it running. (Writing to the closed pipe cannot be the signal: .NET's console stream swallows broken pipes.)
# Keep this file ASCII: it travels through the console input code page.
if (-not $Interval) { $Interval = 3 }
if (-not $MaxLife) { $MaxLife = 600 }
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$out = [Console]::Out
$inv = [Globalization.CultureInfo]::InvariantCulture

function N($s) {
  $d = 0.0
  if ([double]::TryParse("$s".Trim(), [Globalization.NumberStyles]::Float, $inv, [ref]$d)) { return $d }
  return $null
}
function MiB($s) { $v = N $s; if ($null -eq $v) { return $null }; return $v * 1048576 }

$cpu0 = Get-CimInstance Win32_Processor | Select-Object -First 1
$os0 = Get-CimInstance Win32_OperatingSystem
$board = Get-CimInstance Win32_BaseBoard | Select-Object -First 1
$boot = ([DateTimeOffset]$os0.LastBootUpTime).ToUnixTimeSeconds()
$Q = 'index,uuid,name,driver_version,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,temperature.memory,power.draw,power.limit,fan.speed,clocks.current.graphics,clocks.current.memory,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,pstate,clocks_throttle_reasons.active'

# The sshd session this agent lives under: powershell <- cmd <- sshd. When the connection drops, that sshd exits.
$session = $null
$p = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
for ($i = 0; $i -lt 4 -and $p; $i++) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)"
  if ($p -and $p.Name -match '^sshd') { $session = [int]$p.ProcessId; break }
}

$started = Get-Date
$tick = 0
$gpuProcs = @()
$procsCpu = @()
$procsMem = @()
$models = $null

while (((Get-Date) - $started).TotalSeconds -lt $MaxLife) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0

  $gpus = @()
  foreach ($line in @(nvidia-smi "--query-gpu=$Q" --format=csv,noheader,nounits)) {
    $f = $line -split ',\s*'
    if ($f.Count -lt 21) { continue }
    $gpus += @{
      index = [int]$f[0]; uuid = $f[1]; name = ($f[2] -replace '^NVIDIA GeForce ', ''); driver = $f[3]
      util = (N $f[4]); mem_util = (N $f[5]); mem_used = (MiB $f[6]); mem_total = (MiB $f[7])
      temp = (N $f[8]); mem_temp = (N $f[9]); power = (N $f[10]); power_limit = (N $f[11]); fan = (N $f[12])
      clock = (N $f[13]); mem_clock = (N $f[14]); pcie_gen = (N $f[15]); pcie_gen_max = (N $f[16])
      pcie_width = (N $f[17]); pcie_width_max = (N $f[18]); pstate = $f[19]; throttle_raw = $f[20]
    }
  }

  $pinfo = @(Get-CimInstance Win32_PerfFormattedData_Counters_ProcessorInformation)
  $tot = $pinfo | Where-Object { $_.Name -eq '_Total' } | Select-Object -First 1
  $coreRows = @($pinfo | Where-Object { $_.Name -match '^\d+,\d+$' } |
    Sort-Object { [int]($_.Name -split ',')[0] }, { [int]($_.Name -split ',')[1] })
  $cores = @($coreRows | ForEach-Object { [math]::Min(100, [double]$_.PercentProcessorUtility) })
  $freq = $null
  if ($coreRows.Count) {
    $freq = [math]::Round((($coreRows | ForEach-Object { [double]$_.ProcessorFrequency * [double]$_.PercentProcessorPerformance / 100 }) |
      Measure-Object -Average).Average)
  }

  # Win32_OperatingSystem cost 22 ms of CPU a tick for one number: free memory is AvailableBytes here, and the
  # total is read once at start (RESEARCH.md). Page file and disk space change slowly: every 5th tick.
  $pm = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
  if ($tick % 5 -eq 0) {
    $pf = @(Get-CimInstance Win32_PageFileUsage)
    $disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
      @{ name = ("$($_.DeviceID) $($_.VolumeName)").Trim(); total = [double]$_.Size; free = [double]$_.FreeSpace } })
  }
  $memTotal = [double]$os0.TotalVisibleMemorySize * 1024
  $mem = @{
    total = $memTotal; used = $memTotal - [double]$pm.AvailableBytes
    committed = [double]$pm.CommittedBytes; commit_limit = [double]$pm.CommitLimit; cached = [double]$pm.CacheBytes
    swap_total = [double](($pf | Measure-Object AllocatedBaseSize -Sum).Sum) * 1048576
    swap_used = [double](($pf | Measure-Object CurrentUsage -Sum).Sum) * 1048576
  }

  $dio = Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -Filter "Name='_Total'"
  $nic = @(Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface)

  if ($tick % 3 -eq 0) {
    $apps = @{}
    foreach ($line in @(nvidia-smi --query-compute-apps=pid,process_name,gpu_uuid --format=csv,noheader,nounits)) {
      $f = $line -split ',\s*'
      if ($f.Count -ge 3) { $apps[[int]$f[0]] = @{ name = (Split-Path ($f[1..($f.Count - 2)] -join ',') -Leaf); uuid = $f[-1] } }
    }
    $vram = @{}
    foreach ($r in @(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory)) {
      if ($r.Name -match '^pid_(\d+)_') {
        $p = [int]$Matches[1]
        if ([double]$r.DedicatedUsage -gt [double]$vram[$p]) { $vram[$p] = [double]$r.DedicatedUsage }
      }
    }
    $gp = @()
    foreach ($p in $apps.Keys) { $gp += @{ pid = $p; name = $apps[$p].name; uuid = $apps[$p].uuid; mem = [double]$vram[$p] } }
    $gpuProcs = @($gp | Sort-Object { -$_.mem } | Select-Object -First 8)

    $pp = @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object { $_.Name -ne '_Total' -and $_.Name -ne 'Idle' })
    $row = { @{ pid = [int]$_.IDProcess; name = $_.Name; cpu = [double]$_.PercentProcessorTime; mem = [double]$_.WorkingSetPrivate } }
    $procsCpu = @($pp | Sort-Object PercentProcessorTime -Descending | Select-Object -First 6 | ForEach-Object $row)
    $procsMem = @($pp | Sort-Object WorkingSetPrivate -Descending | Select-Object -First 6 | ForEach-Object $row)
  }
  if ($tick % 5 -eq 0) {
    $raw = curl.exe -s -m 1 http://127.0.0.1:11434/api/ps
    $models = $null
    if ($raw) { $models = @((ConvertFrom-Json "$raw").models | ForEach-Object { @{ name = $_.name; vram = $_.size_vram; size = $_.size } }) }
  }

  $gpuW = $null
  if ($gpus.Count) { $gpuW = [math]::Round((($gpus | ForEach-Object { $_.power }) | Measure-Object -Sum).Sum, 2) }
  $sample = @{
    t = $now; kind = 'win'; host = $env:COMPUTERNAME; os = "$($os0.Caption) $($os0.Version)" -replace '^Microsoft ', ''
    model = "$($board.Manufacturer) $($board.Product)".Trim(); uptime = $now - $boot
    cpu = @{ model = "$($cpu0.Name)".Trim(); util = [math]::Min(100, [double]$tot.PercentProcessorUtility); cores = $cores
             kinds = @(); load = $null; temp = $null; freq_mhz = $freq
             ncores = [int]$cpu0.NumberOfCores; nthreads = [int]$cpu0.NumberOfLogicalProcessors }
    mem = $mem; gpus = $gpus
    power = @{ gpu_w = $gpuW }
    temps = @(); fans = @(); disks = $disks
    disk_io = @{ r = [double]$dio.DiskReadBytesPersec; w = [double]$dio.DiskWriteBytesPersec; busy = [double]$dio.PercentDiskTime }
    net = @{ rx = [double](($nic | Measure-Object BytesReceivedPersec -Sum).Sum); tx = [double](($nic | Measure-Object BytesSentPersec -Sum).Sum) }
    procs_cpu = $procsCpu; procs_mem = $procsMem; gpu_procs = $gpuProcs; models = $models
    agent_ms = [int]$sw.ElapsedMilliseconds
  }
  if ($session -and -not (Get-Process -Id $session)) { exit }
  try { $out.WriteLine(($sample | ConvertTo-Json -Depth 6 -Compress)); $out.Flush() } catch { exit }
  $tick++
  Start-Sleep -Milliseconds ([math]::Max(200, $Interval * 1000 - $sw.ElapsedMilliseconds))
}
