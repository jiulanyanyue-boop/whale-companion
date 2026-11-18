# Whale Agent v2 · APK 打包脚本（debug 签名）
#
# 用法：
#   pwsh -File tools\build-apk.ps1            # 增量：复用 .verify\out\classes
#   pwsh -File tools\build-apk.ps1 -Clean     # 完整重建：先跑 verify-kotlin.ps1 再打包
#
# 前置（本机已具备，见 docs/IMPLEMENTATION-STATUS.md）：
#   <ANDROID_HOME>\jdk17                        JDK 17（Zulu 17.0.13）
#   <ANDROID_HOME>\platforms\android-34         android.jar
#   <ANDROID_HOME>\build-tools\34.0.0           aapt2 / zipalign / apksigner
#   <ANDROID_HOME>\cmdline-tools\latest\lib\r8.jar   D8 8.2.33（见下方「为什么不用 build-tools 的 d8」）
#   .verify\out\classes                     tools\verify-kotlin.ps1 产出的 main class
#
# ⚠️ 本脚本产出的 APK 使用 **debug keystore**，仅供本机安装调试，
#    证书指纹与密码都不固定，**不可用于发布**，也不能上架任何应用商店。
#
# 构建链路（沿用 <ANDROID_HOME>\whale-agent\build.ps1 已验证的 5/6、6/6 两段）：
#   aapt2 compile → aapt2 link（生成 R + base.apk）→ D8 转 dex → 合并进 apk
#   → zipalign 对齐 → apksigner 签名 → apksigner verify

param(
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

# ── 路径 ────────────────────────────────────────────────────
$Repo    = Split-Path -Parent $PSScriptRoot
$App     = "$Repo\device\app"
$Src     = "$App\src\main"
$Work    = "$Repo\.verify"
$Classes = "$Work\out\classes"
$Gen     = "$Work\out\gen"
$Stage   = "$Work\apk-stage"
$Dist    = "$Repo\dist"
$ApkName = 'WhaleAgent-debug.apk'
$Final   = "$Dist\$ApkName"

$JDK   = $env:JAVA_HOME
$BT    = (Join-Path $env:ANDROID_HOME 'build-tools/34.0.0')
$PLAT  = (Join-Path $env:ANDROID_HOME 'platforms/android-34/android.jar')
$Deps  = "$Work\deps"
$R8Jar = (Join-Path $env:ANDROID_HOME 'cmdline-tools/latest/lib/r8.jar')

$KS      = "$Work\debug.keystore"
$KSPass  = "$Work\debug-keystore.pass"
$KSAlias = 'whaleagent-debug'

$env:JAVA_HOME = $JDK
$env:PATH = "$JDK\bin;$env:PATH"

function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  OK   $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "  FAIL $m" -ForegroundColor Red; exit 1 }
function Warn($m) { Write-Host "  WARN $m" -ForegroundColor Yellow }

# ── 0. 前置检查 ────────────────────────────────────────────
Step '0/6 检查工具链'
foreach ($p in @(
    "$JDK\bin\java.exe", "$JDK\bin\keytool.exe", "$JDK\bin\jar.exe",
    "$BT\aapt2.exe", "$BT\zipalign.exe", "$BT\apksigner.bat",
    $PLAT, $R8Jar
)) {
    if (-not (Test-Path $p)) { Bad "缺少工具链组件: $p" }
}
Ok 'JDK / aapt2 / zipalign / apksigner / android.jar / r8.jar 齐全'

if (-not (Test-Path $Deps)) {
    Bad "缺少第三方依赖目录 .verify\deps；请先运行 pwsh -File tools\verify-kotlin.ps1"
}

# ── 1. 准备 Kotlin 产物 ────────────────────────────────────
Step '1/6 准备 Kotlin 编译产物'
if ($Clean) {
    # 完整重建：先清产物，再交给 verify-kotlin.ps1 重新编译并重跑单测。
    Remove-Item "$Work\out" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $Final -Force -ErrorAction SilentlyContinue
    Ok '已清理 .verify\out 与 dist 产物'
}

$needKotlin = -not (Test-Path $Classes)
if (-not $needKotlin) {
    if (-not (Test-Path "$Classes\cn\whale\agent\device\R.class")) { $needKotlin = $true }
}
if ($Clean -or $needKotlin) {
    Write-Host '  调用 tools\verify-kotlin.ps1 重新编译并跑单测…'
    & pwsh -NoProfile -File "$Repo\tools\verify-kotlin.ps1"
    if ($LASTEXITCODE -ne 0) { Bad "verify-kotlin.ps1 失败（exit $LASTEXITCODE）" }
}
if (-not (Test-Path $Classes)) { Bad "缺少 $Classes" }

$classCount = (Get-ChildItem $Classes -Recurse -Filter *.class -ErrorAction SilentlyContinue).Count
if ($classCount -le 0) { Bad 'Kotlin 产物为空，请先运行 tools\verify-kotlin.ps1' }
Ok "$classCount 个 main class（复用 .verify\out\classes）"

# ── 2. 编译资源 ────────────────────────────────────────────
Step '2/6 编译资源 (aapt2 compile)'
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
$resZip = "$Stage\res.zip"
Remove-Item $resZip -Force -ErrorAction SilentlyContinue
& "$BT\aapt2.exe" compile --dir "$Src\res" -o $resZip
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $resZip)) { Bad '资源编译失败（aapt2 compile）' }
Ok 'res.zip'

# ⚠️ 依赖库（aar）的资源也必须编译并链接进来。
#
# 这一步漏了会出现什么：`androidx.lifecycle.ViewTreeLifecycleOwner` 这类类会引用
# `R$id.view_tree_lifecycle_owner`，而那个 R 类由 aapt2 从 **aar 的 res/** 生成。
# 我们只链接自己的 res 时，aar 的 R 类根本不存在 → 真机报
#   ClassNotFoundException: androidx.lifecycle.runtime.R$id
# 然后父类链断裂，整个 Activity 加载不到。**编译、单测、dex 反汇编全都不报错**，
# 只有真机（华为 CTR-AL20 / API 31）才暴露。
#
# 这里按 `build.gradle.kts` 的 implementation 列表，把每个依赖 aar 的 res/ 编译成
# 独立的 .flata 包，再把它们的 R.txt 一并交给 aapt2 link（--extra-packages）。
Add-Type -AssemblyName System.IO.Compression.FileSystem

#: 需要并入资源的依赖 aar（与 runtimeJars 对应的上层 aar，按需增补）。
$resAars = @(
    'lifecycle-runtime-android-2.8.7.aar',
    'lifecycle-viewmodel-android-2.8.7.aar',
    'lifecycle-viewmodel-savedstate-2.8.7.aar',
    'savedstate-1.2.1.aar',
    'core-1.15.0.aar',
    'activity-1.10.0.aar',
    'camera-core-1.4.1.aar',
    'camera-view-1.4.1.aar',
    'camera-video-1.4.1.aar',
    'exifinterface-1.3.7.aar',
    'room-runtime-2.6.1.aar',
    'security-crypto-1.1.0-alpha06.aar'
) | ForEach-Object { "$Deps\$_" } | Where-Object { Test-Path $_ }

$depResZips = @()
$depRTxts = @()
foreach ($aar in $resAars) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($aar)
    $ex = "$Stage\deps_res\$name"
    Remove-Item $ex -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $ex | Out-Null
    try {
        [System.IO.Compression.ZipFile]::ExtractToDirectory($aar, $ex)
    } catch {
        Write-Host "  跳过 $name（解包失败）" -ForegroundColor Yellow
        continue
    }

    # 有 res/ 才需要编译；R.txt 无论有无 res 都要（它是 R 类的定义来源）。
    $resDir = Join-Path $ex 'res'
    if (Test-Path $resDir) {
        $zip = "$Stage\deps_res\$name.zip"
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        & "$BT\aapt2.exe" compile --dir $resDir -o $zip 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0 -and (Test-Path $zip)) { $depResZips += $zip }
    }
    $rtxt = Join-Path $ex 'R.txt'
    if (Test-Path $rtxt) { $depRTxts += $rtxt }
}
Ok "依赖资源：$($depResZips.Count) 个 res 包、$($depRTxts.Count) 个 R.txt"

# ── 3. 链接资源 + 生成 R ───────────────────────────────────
Step '3/6 链接资源 (aapt2 link)'
# aapt2 link 要求 <manifest> 带 package 属性；AGP 8 用 namespace 代替，
# 所以这里生成一份临时 manifest（不改仓库里的源文件）。
$manifestSrc  = "$Src\AndroidManifest.xml"
$manifestTemp = "$Stage\AndroidManifest.aapt2.xml"
(Get-Content $manifestSrc -Raw -Encoding UTF8) `
    -replace '<manifest ', '<manifest package="cn.whale.agent.device" ' |
    Set-Content $manifestTemp -Encoding UTF8 -NoNewline

$baseApk = "$Stage\base.apk"
Remove-Item $baseApk -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Gen | Out-Null

# 合并所有 R.txt（自己的 + 依赖的），让 aapt2 为每个包生成对应的 R 类。
# aapt2 link 用 `--stable-ids`/`--extra-packages` 的方式比较绕，
# 更直接的做法是：把自己的 res.zip 与依赖的 res 包一起喂进去，
# 并显式声明要生成 R 的包名前缀（--extra-packages）。
$linkArgs = @(
    'link', '-o', $baseApk, '-I', $PLAT,
    '--manifest', $manifestTemp,
    '--java', $Gen,
    '--min-sdk-version', '26', '--target-sdk-version', '36',
    '--auto-add-overlay'
)
# 依赖的包名（aapt2 会为这些包生成 R 类；缺一不可，否则真机 ClassFound）。
# 注意：`androidx.lifecycle.runtime` 是 `lifecycle-runtime-android` 的资源包名。
foreach ($pkg in @(
    'androidx.lifecycle.runtime',
    'androidx.lifecycle.viewmodel',
    'androidx.lifecycle.viewmodel.savedstate',
    'androidx.savedstate',
    'androidx.core',
    'androidx.activity',
    'androidx.camera.core',
    'androidx.camera.view',
    'androidx.room',
    'androidx.security.crypto'
)) {
    $linkArgs += @('--extra-packages', $pkg)
}
$linkArgs += $resZip
$linkArgs += $depResZips

& "$BT\aapt2.exe" @linkArgs
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $baseApk)) { Bad '资源链接失败（aapt2 link）' }
Ok "base.apk（minSdk 26 / targetSdk 36，含 $($depResZips.Count) 个依赖资源包）"

# ── 4. D8 转 dex ────────────────────────────────────────────
Step '4/6 转 dex (D8)'
$std = "$Deps\kotlin-stdlib-2.0.21.jar"
if (-not (Test-Path $std)) { Bad "缺少 $std" }

# 参与打包的第三方 runtime 依赖（按 build.gradle.kts 的 implementation 对应）。
# 排除项说明：
#   - *.aar 解出的 *-classes.jar 是真正的运行时 class；
#   - 不带 -classes 的 core-1.15.0.aar 等只在编译期用，不进 dex；
#   - kotlin-serialization-compiler-plugin* 是编译器插件，不进 dex；
#   - junit / hamcrest 是测试依赖，不进 dex；
#   - guava 仅供 desugaring 参考，本构建未用到，不进 dex。
$runtimeJars = @(
    'annotation-1.9.1.jar',
    'annotation-experimental-1.4.1-classes.jar',
    'activity-1.10.0-classes.jar',
    'camera-camera2-1.4.1-classes.jar',
    'camera-core-1.4.1-classes.jar',
    'camera-lifecycle-1.4.1-classes.jar',
    'camera-view-1.4.1-classes.jar',
    'camera-video-1.4.1-classes.jar',
    'lifecycle-livedata-classes.jar',
    'lifecycle-livedata-core-classes.jar',
    'sqlite-framework-2.4.0-classes.jar',
    'versionedparcelable-1.1.1-classes.jar',
    'paging-common-3.3.2-classes.jar',
    'exifinterface-1.3.7-classes.jar',
    'collection-jvm-1.4.0.jar',
    'concurrent-futures-1.1.0.jar',
    'core-1.15.0-classes.jar',
    'core-ktx-1.15.0-classes.jar',
    # ⚠️ 下面四个是 `androidx.activity.ComponentActivity` 的**直接父类型与接口来源**。
    # 漏掉它们时 D8 仍能产出 dex（它只是把引用写进去），但 ART 在 class linking
    # 阶段解析父类/接口失败，整个类被拒绝加载，表现为：
    #   ClassNotFoundException: androidx.activity.ComponentActivity
    # 这条是**真机（华为 CTR-AL20 / API 31）**测出来的——编译、单测、dex 反汇编全都正常，
    # 只有真机运行才暴露。缺一个就会让 `MainActivity` 一起加载不到。
    'lifecycle-runtime-android-2.8.7-classes.jar',
    'lifecycle-viewmodel-android-2.8.7-classes.jar',
    'lifecycle-viewmodel-savedstate-2.8.7-classes.jar',
    'savedstate-1.2.1-classes.jar',
    'lifecycle-common-jvm-2.8.7.jar',
    'lifecycle-service-2.8.7-classes.jar',
    'arch-core-common-2.2.0.jar',
    'arch-core-runtime-classes.jar',
    # kotlinx-coroutines：Stdlib 不含协程，而 lifecycle/activity 的 Kotlin 版本依赖它。
    'kotlinx-coroutines-core-jvm-1.9.0.jar',
    'kotlinx-coroutines-android-1.9.0.jar',
    'tracing-classes.jar',
    'listenablefuture-real.jar',
    'kotlinx-serialization-core-jvm-1.7.3.jar',
    'kotlinx-serialization-json-jvm-1.7.3.jar',
    # M05 配对扫码：zxing 二维码解码（PairingActivity 依赖）。
    'zxing-core-3.5.3.jar',
    'okhttp-4.12.0.jar',
    'okio-jvm-3.6.0.jar',
    'room-common-2.6.1.jar',
    'room-runtime-2.6.1-classes.jar',
    'room-ktx-classes.jar',
    'sqlite-2.4.0-classes.jar',
    'security-crypto-classes.jar'
) | ForEach-Object { "$Deps\$_" } | Where-Object { Test-Path $_ }

# 复核：清单里每个 jar 都必须真实存在。静默跳过会让「漏打包」再次发生，
# 而那样只有真机能发现（编译与单测都不会报错）。
$missingRuntime = @(
    @(
        'annotation-1.9.1.jar','annotation-experimental-1.4.1-classes.jar','activity-1.10.0-classes.jar',
        'camera-camera2-1.4.1-classes.jar','camera-core-1.4.1-classes.jar','camera-lifecycle-1.4.1-classes.jar',
        'camera-view-1.4.1-classes.jar','camera-video-1.4.1-classes.jar',
    'lifecycle-livedata-classes.jar',
    'lifecycle-livedata-core-classes.jar',
    'sqlite-framework-2.4.0-classes.jar',
    'versionedparcelable-1.1.1-classes.jar',
    'paging-common-3.3.2-classes.jar','exifinterface-1.3.7-classes.jar','collection-jvm-1.4.0.jar','concurrent-futures-1.1.0.jar',
        'core-1.15.0-classes.jar','core-ktx-1.15.0-classes.jar',
        'lifecycle-runtime-android-2.8.7-classes.jar','lifecycle-viewmodel-android-2.8.7-classes.jar',
        'lifecycle-viewmodel-savedstate-2.8.7-classes.jar','savedstate-1.2.1-classes.jar',
        'lifecycle-common-jvm-2.8.7.jar','lifecycle-service-2.8.7-classes.jar',
        'arch-core-common-2.2.0.jar',
        'arch-core-runtime-classes.jar','kotlinx-coroutines-core-jvm-1.9.0.jar',
        'kotlinx-coroutines-android-1.9.0.jar','tracing-classes.jar','listenablefuture-real.jar',
        'kotlinx-serialization-core-jvm-1.7.3.jar','kotlinx-serialization-json-jvm-1.7.3.jar',
        'zxing-core-3.5.3.jar','okhttp-4.12.0.jar','okio-jvm-3.6.0.jar',
        'room-common-2.6.1.jar','room-runtime-2.6.1-classes.jar','room-ktx-classes.jar','sqlite-2.4.0-classes.jar','security-crypto-classes.jar'
    ) | Where-Object { -not (Test-Path "$Deps\$_") }
)
if ($missingRuntime.Count -gt 0) {
    foreach ($m in $missingRuntime) { Write-Host "  缺少依赖 jar: $m" -ForegroundColor Red }
    Bad "打包依赖缺失（$($missingRuntime.Count) 个）；缺任一个都会让真机 ClassNotFoundException"
}

foreach ($j in $runtimeJars) { Ok "dep $([System.IO.Path]::GetFileName($j))" }

$dexOut = "$Stage\dex"
Remove-Item $dexOut -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $dexOut | Out-Null

$classFiles = (Get-ChildItem $Classes -Recurse -Filter *.class).FullName

# ⚠️ 必须用 @参数文件，不能把 class 列表直接拼到命令行上。
# Windows CreateProcess 命令行上限约 32767 字符；本工程 264 个 class 的绝对路径
# 拼起来必然爆掉，报错只有一句 "The command line is too long."，
# 看起来像编译失败，其实编译早就过了。
# （与 whale-agent\build.ps1 的 4/6、5/6 同源经验）
$d8Args = "$Stage\d8_args.txt"
Set-Content -Path $d8Args -Encoding ASCII -Value (@(
    '--min-api', '26', '--lib', $PLAT, '--output', $dexOut
) + $classFiles + @($std) + $runtimeJars)

# ⚠️ 为什么用 cmdline-tools 里的 r8.jar 而不是 build-tools\34.0.0\d8.bat：
# build-tools 自带的是 D8 8.2.2-dev，解析部分 class 的 `EnclosingMethod` 属性时会
# 内部崩溃（R8 8.2.2 的已知缺陷）：
#   Error in core-1.15.0-classes.jar:android/support/v4/os/ResultReceiver$1.class:
#   java.lang.NullPointerException: Cannot invoke "String.length()" because "<parameter1>" is null
#   Compilation failed with an internal error.
# 同样的 class 用 cmdline-tools\latest\lib\r8.jar（D8 8.2.33）能正常产出 dex。
# 两个 jar 都是本机既有组件，不引入任何新依赖。
# 另外这两个 d8 版本对 Kotlin 2.0.21 的 metadata 都会打 "malformed kotlin.Metadata"
# 警告（R8 生成的版本早于 Kotlin 2.0），但只是 info/warning，不影响 dex 生成。
$r8Out = & "$JDK\bin\java.exe" -cp $R8Jar com.android.tools.r8.D8 "@$d8Args" 2>&1 | Out-String
$d8Code = $LASTEXITCODE
if ($d8Code -ne 0) {
    ($r8Out -split "`n" | Where-Object { $_ -match 'Error|Exception' } | Select-Object -First 8) |
        ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    Bad "D8 失败（exit $d8Code）"
}

$dexFiles = @(Get-ChildItem $dexOut -Filter *.dex -ErrorAction SilentlyContinue)
if ($dexFiles.Count -eq 0) { Bad 'dex 生成失败（没有产出 .dex）' }
$dexFiles | ForEach-Object { Ok "$($_.Name) $([math]::Round($_.Length / 1KB, 0)) KB" }

# ── 5. 合并 dex + 对齐 ────────────────────────────────────
Step '5/6 合并 dex 并 zipalign'
Copy-Item "$dexOut\classes.dex" "$Stage\classes.dex" -Force

# ⚠️ `classes.dex` 在 APK 里**必须 Stored（未压缩）**。
#
# 这不是优化选项，是加载前提：ART 直接 mmap 映射未压缩的 dex；
# 若 dex 被 Deflate 压缩，`DexPathList` 找不到类，表现为：
#   java.lang.ClassNotFoundException:
#     Didn't find class "…MainActivity" on path: DexPathList[[zip file "…/base.apk"]]
# 而且**极其误导**——APK 里 dexdump 明明能查到那些类，编译/单测也全绿，
# 只有真机才暴露。本项就是靠真机（华为 CTR-AL20 / Android 12）测出来的。
#
# 实现上**不能**用 `jar uf`（默认 Deflate），也**不能**用 ZipArchive 的
# `Update` 模式往已有 zip 里塞（那样改写中央目录后 ART 的 zip 读取器会解析失败，
# 表现同样是 ClassNotFoundException）。这里**全新重建**整个 zip：
# 逐条复制原条目，dex 用 NoCompression，保证结构与顺序干净。
$rebuilt = "$Stage\rebuilt.apk"
Remove-Item $rebuilt -Force -ErrorAction SilentlyContinue

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$src = [System.IO.Compression.ZipFile]::OpenRead($baseApk)
$dst = [System.IO.Compression.ZipFile]::Open($rebuilt, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    # 1) 先放 classes.dex（Stored）——放在最前，避免某些 ART 实现的惰性查找问题。
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $dst,
        "$Stage\classes.dex",
        'classes.dex',
        [System.IO.Compression.CompressionLevel]::NoCompression
    ) | Out-Null

    # 2) 其余条目（resources.arsc 保持 Stored，其余按原样 Deflate）。
    foreach ($entry in $src.Entries) {
        if ($entry.FullName -eq 'classes.dex') { continue }

        $stream = $entry.Open()
        try {
            # resources.arsc 也必须 Stored（Android 要求直接映射）。
            $level = if ($entry.FullName -eq 'resources.arsc') {
                [System.IO.Compression.CompressionLevel]::NoCompression
            } else {
                [System.IO.Compression.CompressionLevel]::Optimal
            }
            $new = $dst.CreateEntry($entry.FullName, $level)

            # 保留原始时间戳，减少无意义差异。
            if ($entry.LastWriteTime) { $new.LastWriteTime = $entry.LastWriteTime }

            $out = $new.Open()
            try {
                $stream.CopyTo($out)
            } finally {
                $out.Dispose()
            }
        } finally {
            $stream.Dispose()
        }
    }
} finally {
    $dst.Dispose()
    $src.Dispose()
}

Copy-Item $rebuilt $baseApk -Force

# 复核：dex 与 arsc 都必须 Stored，否则装上去必然 ClassNotFoundException。
$zipCheck = [System.IO.Compression.ZipFile]::OpenRead($baseApk)
try {
    foreach ($required in @('classes.dex', 'resources.arsc')) {
        $entry = $zipCheck.Entries | Where-Object { $_.FullName -eq $required } | Select-Object -First 1
        if ($null -eq $entry) { Bad "apk 里没有 $required" }
        if ($entry.CompressedLength -ne $entry.Length) {
            Bad "$required 仍是压缩的（$($entry.CompressedLength) / $($entry.Length)）；真机会加载失败"
        }
    }
} finally {
    $zipCheck.Dispose()
}
Ok 'classes.dex + resources.arsc 均 Stored 未压缩（可被 ART mmap）'

$alignedApk = "$Stage\aligned.apk"
Remove-Item $alignedApk -Force -ErrorAction SilentlyContinue
& "$BT\zipalign.exe" -f -p 4 $baseApk $alignedApk
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $alignedApk)) { Bad 'zipalign 失败' }
Ok 'zipalign 4 字节对齐完成'

# ── 6. 签名 + 验证 ────────────────────────────────────────
Step '6/6 签名（debug）并验证'
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

# debug keystore：首次运行时生成到 .verify（已被 .gitignore 忽略，不进仓库）。
# 密码随机生成后写入 .verify\debug-keystore.pass，脚本与源码里都不出现密码字面量。
if (-not (Test-Path $KS)) {
    $bytes = New-Object 'System.Byte[]' 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $password = ([System.BitConverter]::ToString($bytes) -replace '-', '')
    Set-Content -Path $KSPass -Value $password -Encoding ASCII -NoNewline
    & "$JDK\bin\keytool.exe" -genkeypair -v `
        -keystore $KS -alias $KSAlias `
        -keyalg RSA -keysize 2048 -validity 10000 `
        -storepass $password -keypass $password `
        -dname 'CN=Whale Agent Debug, OU=Debug Only, O=Whale Agent, L=Local, S=Local, C=CN' 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $KS)) { Bad 'debug keystore 生成失败' }
    Warn '已生成 debug keystore：仅供本机调试，不可用于发布'
} else {
    Ok '复用已有 debug keystore（.verify\debug.keystore）'
}

if (-not (Test-Path $KSPass)) { Bad "缺少 debug keystore 密码文件 $KSPass" }
$password = (Get-Content $KSPass -Raw).Trim()

Remove-Item $Final -Force -ErrorAction SilentlyContinue
& "$BT\apksigner.bat" sign `
    --ks $KS --ks-key-alias $KSAlias `
    --ks-pass pass:$password --key-pass pass:$password `
    --out $Final $alignedApk
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Final)) { Bad 'apksigner sign 失败' }

$verifyOut = & "$BT\apksigner.bat" verify --verbose $Final 2>&1 | Out-String
$verifyCode = $LASTEXITCODE
$certsOut = & "$BT\apksigner.bat" verify --print-certs $Final 2>&1 | Out-String

if ($verifyCode -ne 0) {
    Write-Host $verifyOut
    Bad "apksigner verify 失败（exit $verifyCode）"
}
if ($verifyOut -notmatch 'Verifies') {
    Write-Host $verifyOut
    Bad 'apksigner verify 未返回 Verifies'
}
Ok 'apksigner verify 通过（v1/v2 签名已验证）'

# ── 版本号校验 ────────────────────────────────────────────
# 本仓库不走 Gradle，aapt2 只读 AndroidManifest.xml：
# 版本号必须同时写在 gradle 与 manifest 两处，否则打出的 APK 会显示「版本未知」，
# 影响系统升级判断和崩溃上报。这里用 badging 回读产物，杜绝「版本号为空却报成功」。
$badgingOut = & "$BT\aapt2.exe" dump badging $Final 2>&1 | Out-String
$vCodeMatch = [regex]::Match($badgingOut, "versionCode='([^']*)'")
$vNameMatch = [regex]::Match($badgingOut, "versionName='([^']*)'")
if (-not $vCodeMatch.Success -or $vCodeMatch.Groups[1].Value.Trim() -eq '') {
    Bad 'APK 的 versionCode 为空（manifest 缺少 android:versionCode）'
}
if (-not $vNameMatch.Success -or $vNameMatch.Groups[1].Value.Trim() -eq '') {
    Bad 'APK 的 versionName 为空（manifest 缺少 android:versionName）'
}
$apkVersionCode = $vCodeMatch.Groups[1].Value.Trim()
$apkVersionName = $vNameMatch.Groups[1].Value.Trim()
Ok "版本号校验通过：versionCode=$apkVersionCode versionName=$apkVersionName"

$pkgMatch = [regex]::Match($badgingOut, "package: name='([^']*)'")
$minSdkMatch = [regex]::Match($badgingOut, "sdkVersion:'([^']*)'")
$targetSdkMatch = [regex]::Match($badgingOut, "targetSdkVersion:'([^']*)'")
$apkPackage = if ($pkgMatch.Success) { $pkgMatch.Groups[1].Value } else { '?' }
$apkMinSdk = if ($minSdkMatch.Success) { $minSdkMatch.Groups[1].Value } else { '?' }
$apkTargetSdk = if ($targetSdkMatch.Success) { $targetSdkMatch.Groups[1].Value } else { '?' }
Ok "包名与 SDK：$apkPackage / minSdk $apkMinSdk / targetSdk $apkTargetSdk"

# ── 7/7 真机等价校验：dex 引用的 androidx 类必须真的在 dex 里 ─────────
#
# 这道校验是本轮真机调试（华为 CTR-AL20 / API 31）换来的教训：
# 下面每一类问题都**不会**在编译、单测、dex 反汇编里报错，
# 只有装到真机才炸，而且现场表现极具误导性
# （`ClassNotFoundException: <我们的 MainActivity>`，真凶却是父类）。
#
# 已踩过的坑（都已修，这里做回归防线）：
#   1. `classes.dex` 被 Deflate 压缩 → ART 无法 mmap → 所有类都「找不到」；
#   2. 打包清单漏了 `lifecycle-viewmodel` / `savedstate` 等传递依赖 jar；
#   3. 依赖 aar 的 `res/` 没参与 aapt2 link → `…runtime.R$id` 这类 R 类不存在；
#   4. `--extra-packages` 少声明一个包名 → 对应包的 R 类不生成；
#   5. `javac` 只编译了第一个 R.java → 依赖包的 R 类全部缺失。
#
# 这里逐条扫 dex 里对 `androidx.*` 类的**引用**，与 dex 里实际**定义**的类集合比对。
# 缺任何一个都直接判构建失败，避免「看起来能装、一跑就崩」的包流出去。
Step '7/7 真机等价校验（dex 类引用完整性）'
$dexCheckTmp = "$Stage\dexcheck"
Remove-Item $dexCheckTmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $dexCheckTmp | Out-Null
Copy-Item $Final "$dexCheckTmp\a.zip"
Expand-Archive "$dexCheckTmp\a.zip" -DestinationPath $dexCheckTmp -Force
$dexPath = "$dexCheckTmp\classes.dex"

$dumpLines = (& "$BT\dexdump.exe" -f $dexPath 2>&1) -split "`n"
# dex 里**定义**的类（Class descriptor 行）
$defined = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($line in $dumpLines) {
    $m = [regex]::Match($line, "Class descriptor  : 'L([^;]+);'")
    if ($m.Success) { [void]$defined.Add($m.Groups[1].Value) }
}
# dex 里**引用**的 androidx 类（字段/方法签名里出现的 Lxxx;）
$referenced = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($line in $dumpLines) {
    foreach ($mm in [regex]::Matches($line, 'L(androidx/[A-Za-z0-9_$/]+);')) {
        [void]$referenced.Add($mm.Groups[1].Value)
    }
}
$missing = @($referenced | Where-Object { -not $defined.Contains($_) } | Sort-Object)
Remove-Item $dexCheckTmp -Recurse -Force -ErrorAction SilentlyContinue

if ($missing.Count -gt 0) {
    Write-Host "  ✗ dex 引用了 $($missing.Count) 个未定义的 androidx 类（真机必然 ClassNotFoundException）：" -ForegroundColor Red
    $missing | Select-Object -First 12 | ForEach-Object { Write-Host "      L$($_);" -ForegroundColor Red }
    if ($missing.Count -gt 12) { Write-Host "      …还有 $($missing.Count - 12) 个" -ForegroundColor Red }
    Bad "dex 类引用不完整：补 runtimeJars / --extra-packages 后重试（详见上方清单）"
}
Ok "dex 类引用完整（定义 $($defined.Count) 个类，androidx 引用 $($referenced.Count) 个全部可解析）"

# ── 汇总 ──────────────────────────────────────────────────
$sizeKB = [math]::Round((Get-Item $Final).Length / 1KB, 0)
$sizeMB = [math]::Round((Get-Item $Final).Length / 1MB, 2)
$sha = (Get-FileHash $Final -Algorithm SHA256).Hash

Write-Host ''
Write-Host '================ 构建结果 ================' -ForegroundColor Cyan
Write-Host "APK 路径 : $Final"
Write-Host "大小     : $sizeKB KB ($sizeMB MB)"
Write-Host "版本     : versionCode=$apkVersionCode versionName=$apkVersionName"
Write-Host "包名     : $apkPackage（minSdk $apkMinSdk / targetSdk $apkTargetSdk）"
Write-Host "SHA256   : $sha"
Write-Host "签名     : debug keystore（debug.keystore / alias $KSAlias）"
Write-Host "apksigner: $($verifyOut.Trim().Split("`n")[0].Trim())"
$certLine = ($certsOut -split "`n" | Where-Object { $_ -match 'Signer #1 certificate DN' } | Select-Object -First 1)
if ($certLine) { Write-Host "证书     : $($certLine.Trim())" }
Write-Host '⚠️  debug 签名，不可用于发布。' -ForegroundColor Yellow
Write-Host '==========================================' -ForegroundColor Cyan
Write-Host ''

$installHint = "adb install -r `"$Final`""
Write-Host "安装命令 : $installHint" -ForegroundColor Gray
Write-Host "打包成功。" -ForegroundColor Green
exit 0















