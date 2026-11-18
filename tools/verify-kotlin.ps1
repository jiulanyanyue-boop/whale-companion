# Whale Agent v2 · Kotlin 真编译 + 真跑单元测试
#
# 本机没有 Gradle（<ANDROID_HOME> 下只有 Android SDK、JDK17 与 Kotlin 编译器 jar），
# 所以这里不走 AGP，而是用与 <ANDROID_HOME>\whale-agent\build.ps1 相同的「手工工具链」：
#   JDK17 → aapt2 编译资源并生成 R → Kotlin K2JVMCompiler 编译 → JUnit4 真跑。
#
# 用法：
#   .\tools\verify-kotlin.ps1                 # 全量编译 + 跑全部 Kotlin 单测
#   .\tools\verify-kotlin.ps1 -SkipTests      # 只编译
#   .\tools\verify-kotlin.ps1 -Clean          # 清掉 .verify 缓存后重来
#
# 前置（本机已具备）：
#   <ANDROID_HOME>\jdk17                        JDK 17（Zulu 17.0.13）
#   <ANDROID_HOME>\_dl                          kotlin-compiler-embeddable 2.0.21 等 jar
#   <ANDROID_HOME>\platforms\android-34         android.jar
#   <ANDROID_HOME>\build-tools\34.0.0           aapt2
# 第三方依赖由本脚本按需下载到 .verify\deps（首次运行需要网络）。
param(
    [switch]$SkipTests,
    [switch]$Clean
)
$ErrorExpected = 'Continue'
$ErrorActionPreference = 'Continue'
$Repo   = Split-Path -Parent $PSScriptRoot
$App    = "$Repo\device\app"
$Src    = "$App\src\main"
$Java   = "$Src\java\cn\whale\agent\device"
$TestKt = "$App\src\test\java\cn\whale\agent\device"
$Work   = "$Repo\.verify"
$Out    = "$Work\out"
$Deps   = "$Work\deps"
$Gen    = "$Out\gen"
$JDK    = $env:JAVA_HOME
$DL     = $env:WHALE_KOTLIN_LIB
$BT     = (Join-Path $env:ANDROID_HOME 'build-tools/34.0.0')
$PLAT   = (Join-Path $env:ANDROID_HOME 'platforms/android-34/android.jar')
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  OK   $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "  FAIL $m" -ForegroundColor Red; exit 1 }
# ── 前置检查 ────────────────────────────────────────────────
Step '0/6 检查工具链'
foreach ($p in @("$JDK\bin\java.exe", "$BT\aapt2.exe", $PLAT)) {
    if (-not (Test-Path $p)) { Bad "缺少工具链组件: $p" }
}
$compilerJars = Get-ChildItem $DL -Filter '*.jar' -ErrorAction SilentlyContinue
if (-not $compilerJars) { Bad "<ANDROID_HOME>\_dl 下没有 kotlin 编译器 jar" }
Ok "JDK / aapt2 / android.jar / kotlin 编译器齐全"
$env:JAVA_HOME = $JDK
$env:PATH = "$JDK\bin;$env:PATH"
if ($Clean) {
    Remove-Item $Out -Recurse -Force -ErrorAction SilentlyContinue
    Ok '已清理编译产物'
}
New-Item -ItemType Directory -Force -Path "$Out\classes", "$Out\test-classes", $Gen, $Deps | Out-Null
# ── 1. 下载第三方依赖（幂等） ───────────────────────────────
Step '1/6 准备第三方依赖'
$google = 'https://dl.google.com/dl/android/maven2'
$central = 'https://repo.maven.apache.org/maven2'
$aarDeps = @(
    @('core-1.15.0.aar',                "$google/androidx/core/core/1.15.0/core-1.15.0.aar"),
    @('core-ktx-1.15.0.aar',            "$google/androidx/core/core-ktx/1.15.0/core-ktx-1.15.0.aar"),
    @('annotation-experimental-1.4.1.aar', "$google/androidx/annotation/annotation-experimental/1.4.1/annotation-experimental-1.4.1.aar"),
    @('room-runtime-2.6.1.aar',         "$google/androidx/room/room-runtime/2.6.1/room-runtime-2.6.1.aar"),
    @('sqlite-2.4.0.aar',               "$google/androidx/sqlite/sqlite/2.4.0/sqlite-2.4.0.aar"),
    @('activity-1.10.0.aar',            "$google/androidx/activity/activity/1.10.0/activity-1.10.0.aar"),
    @('lifecycle-runtime-2.8.7.aar',    "$google/androidx/lifecycle/lifecycle-runtime/2.8.7/lifecycle-runtime-2.8.7.aar"),
    @('lifecycle-service-2.8.7.aar',    "$google/androidx/lifecycle/lifecycle-service/2.8.7/lifecycle-service-2.8.7.aar"),
    @('camera-core-1.4.1.aar',          "$google/androidx/camera/camera-core/1.4.1/camera-core-1.4.1.aar"),
    @('camera-camera2-1.4.1.aar',       "$google/androidx/camera/camera-camera2/1.4.1/camera-camera2-1.4.1.aar"),
    @('camera-lifecycle-1.4.1.aar',     "$google/androidx/camera/camera-lifecycle/1.4.1/camera-lifecycle-1.4.1.aar"),
    @('camera-view-1.4.1.aar',          "$google/androidx/camera/camera-view/1.4.1/camera-view-1.4.1.aar")
)
$jarDeps = @(
    # ── Room 注解处理器（kapt）依赖链 ─────────────────────────
    #
    # 实测踩过：这些 jar 只要缺一个，kapt 就抛 ClassNotFound，
    # 而 `*_Impl` 生成不出来会让真机上「启动设备服务」直接闪退
    # （Cannot find implementation for ...DeviceDatabase）。
    # 缺任何一个的表现都是英文异常，用户完全无从下手。
    @('room-compiler-2.6.1.jar', "$google/androidx/room/room-compiler/2.6.1/room-compiler-2.6.1.jar"),
    @('room-compiler-processing-2.6.1.jar', "$google/androidx/room/room-compiler-processing/2.6.1/room-compiler-processing-2.6.1.jar"),
    @('room-migration-2.6.1.jar', "$google/androidx/room/room-migration/2.6.1/room-migration-2.6.1.jar"),
    @('kotlin-annotation-processing-embeddable-2.0.21.jar', "$central/org/jetbrains/kotlin/kotlin-annotation-processing-embeddable/2.0.21/kotlin-annotation-processing-embeddable-2.0.21.jar"),
    @('kotlinpoet.jar', "$central/com/squareup/kotlinpoet/1.14.0/kotlinpoet-1.14.0.jar"),
    @('kotlinpoet-javapoet-1.14.2.jar', "$central/com/squareup/kotlinpoet-javapoet/1.14.2/kotlinpoet-javapoet-1.14.2.jar"),
    @('javapoet-1.13.0.jar', "$central/com/squareup/javapoet/1.13.0/javapoet-1.13.0.jar"),
    @('auto-common-1.2.1.jar', "$central/com/google/auto/auto-common/1.2.1/auto-common-1.2.1.jar"),
    @('auto-value-annotations-1.6.3.jar', "$central/com/google/auto/value/auto-value-annotations/1.6.3/auto-value-annotations-1.6.3.jar"),
    @('intellij-annotations-12.0.jar', "$central/com/intellij/annotations/12.0/annotations-12.0.jar"),
    @('sqlite-jdbc-3.42.0.0.jar', "$central/org/xerial/sqlite-jdbc/3.42.0.0/sqlite-jdbc-3.42.0.0.jar"),
    @('commons-codec-1.15.jar', "$central/commons-codec/commons-codec/1.15/commons-codec-1.15.jar"),
    @('annotation-real-1.9.1.jar', "$google/androidx/annotation/annotation-jvm/1.9.1/annotation-jvm-1.9.1.jar"),
    @('zxing-core-3.5.3.jar',                     "$central/com/google/zxing/core/3.5.3/core-3.5.3.jar"),
    @('kotlin-stdlib-2.0.21.jar',              "$central/org/jetbrains/kotlin/kotlin-stdlib/2.0.21/kotlin-stdlib-2.0.21.jar"),
    @('kotlinx-serialization-core-jvm-1.7.3.jar', "$central/org/jetbrains/kotlinx/kotlinx-serialization-core-jvm/1.7.3/kotlinx-serialization-core-jvm-1.7.3.jar"),
    @('kotlinx-serialization-json-jvm-1.7.3.jar', "$central/org/jetbrains/kotlinx/kotlinx-serialization-json-jvm/1.7.3/kotlinx-serialization-json-jvm-1.7.3.jar"),
    @('okhttp-4.12.0.jar',                     "$central/com/squareup/okhttp3/okhttp/4.12.0/okhttp-4.12.0.jar"),
    @('okio-jvm-3.6.0.jar',                    "$central/com/squareup/okio/okio-jvm/3.6.0/okio-jvm-3.6.0.jar"),
    @('annotation-1.9.1.jar',                  "$google/androidx/annotation/annotation/1.9.1/annotation-1.9.1.jar"),
    @('collection-1.4.0.jar',                  "$google/androidx/collection/collection/1.4.0/collection-1.4.0.jar"),
    @('lifecycle-common-jvm-2.8.7.jar',        "$google/androidx/lifecycle/lifecycle-common-jvm/2.8.7/lifecycle-common-jvm-2.8.7.jar"),
    @('room-common-2.6.1.jar',                 "$google/androidx/room/room-common/2.6.1/room-common-2.6.1.jar"),
    @('concurrent-futures-1.1.0.jar',          "$google/androidx/concurrent/concurrent-futures/1.1.0/concurrent-futures-1.1.0.jar"),
    @('listenablefuture-1.0.jar',              "$central/com/google/guava/listenablefuture/9999.0-empty-to-avoid-conflict-with-guava/listenablefuture-9999.0-empty-to-avoid-conflict-with-guava.jar"),
    @('junit-4.13.2.jar',                      "$central/junit/junit/4.13.2/junit-4.13.2.jar"),
    @('hamcrest-core-1.3.jar',                 "$central/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar"),
    @('kotlin-serialization-compiler-plugin-embeddable-2.0.21.jar',
                                               "$central/org/jetbrains/kotlin/kotlin-serialization-compiler-plugin-embeddable/2.0.21/kotlin-serialization-compiler-plugin-embeddable-2.0.21.jar")
)
foreach ($d in @($aarDeps) + @($jarDeps)) {
    $dest = "$Deps\$($d[0])"
    if (Test-Path $dest) { continue }
    try { Invoke-WebRequest -Uri $d[1] -OutFile $dest -TimeoutSec 60 -UseBasicParsing }
    catch { Bad "下载失败: $($d[0])" }
}
# .aar 解出 classes.jar（Kotlin 编译器只认 jar）
Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($aar in Get-ChildItem $Deps -Filter *.aar) {
    $cj = "$Deps\$($aar.BaseName)-classes.jar"
    if (Test-Path $cj) { continue }
    $ex = "$Deps\_aar_$($aar.BaseName)"
    Remove-Item $ex -Recurse -Force -ErrorAction SilentlyContinue
    [System.IO.Compression.ZipFile]::ExtractToDirectory($aar.FullName, $ex)
    if (Test-Path "$ex\classes.jar") { Copy-Item "$ex\classes.jar" $cj -Force }
}
Ok "依赖就绪（$((Get-ChildItem $Deps -Filter '*.jar').Count) 个 jar）"
$kotlinClasspath = ($compilerJars | ForEach-Object { $_.FullName }) -join ';'
$stdlib    = "$Deps\kotlin-stdlib-2.0.21.jar"
$serPlugin = "$Deps\kotlin-serialization-compiler-plugin-embeddable-2.0.21.jar"
$libCp     = ((Get-ChildItem $Deps -Filter '*.jar' |
              Where-Object { $_.Name -notmatch 'compiler-plugin' } |
              ForEach-Object { $_.FullName }) -join ';')
# ── 2. aapt2 编译资源 + 生成 R ─────────────────────────────
Step '2/6 编译资源并生成 R'
$resZip = "$Out\res.zip"
& "$BT\aapt2.exe" compile --dir "$Src\res" -o $resZip
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $resZip)) { Bad '资源编译失败（aapt2 compile）' }
# aapt2 link 要求 <manifest> 带 package 属性；AGP 8 用 namespace 代替，
# 所以这里生成一份临时 manifest（不改仓库里的源文件）。
$manifest = "$Out\AndroidManifest.aapt2.xml"
(Get-Content "$Src\AndroidManifest.xml" -Raw) `
    -replace '<manifest ', '<manifest package="cn.whale.agent.device" ' |
    Set-Content $manifest -Encoding UTF8 -NoNewline
# 依赖库（aar）的资源与 R 定义也必须参与链接。
#
# 为什么必须：`androidx.lifecycle.ViewTreeLifecycleOwner` 等类引用
# `R$id.view_tree_lifecycle_owner`，那个 R 类由 aapt2 从 **aar 的 res/** 生成。
# 只链接自己的 res 时，依赖 R 类不存在 → 真机
#   ClassNotFoundException: androidx.lifecycle.runtime.R$id
# 而且**编译期与单测期都不报错**，只有真机装上去才暴露。
#
# 这里把每个依赖 aar 的 res/ 编译成独立包，并用 `--extra-packages` 声明包名，
# 让 aapt2 一并生成对应的 R.java。与 build-apk.ps1 的处理保持一致。
Add-Type -AssemblyName System.IO.Compression.FileSystem
$depResAars = @(
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
$depResRoot = "$Out\deps-res"
Remove-Item $depResRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $depResRoot | Out-Null
foreach ($aar in $depResAars) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($aar)
    $ex = "$depResRoot\$name"
    New-Item -ItemType Directory -Force -Path $ex | Out-Null
    try { [System.IO.Compression.ZipFile]::ExtractToDirectory($aar, $ex) } catch { continue }
    $resDir = Join-Path $ex 'res'
    if (Test-Path $resDir) {
        $zip = "$depResRoot\$name.zip"
        & "$BT\aapt2.exe" compile --dir $resDir -o $zip 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0 -and (Test-Path $zip)) { $depResZips += $zip }
    }
}
$linkArgs = @(
    'link', '-o', "$Out\base.apk", '-I', $PLAT,
    '--manifest', $manifest, '--java', $Gen,
    '--min-sdk-version', '26', '--target-sdk-version', '36',
    '--auto-add-overlay'
)
foreach ($pkg in @(
    'androidx.lifecycle.runtime', 'androidx.lifecycle.viewmodel', 'androidx.lifecycle.viewmodel.savedstate', 'androidx.savedstate', 'androidx.core', 'androidx.activity',
    'androidx.camera.core', 'androidx.camera.view', 'androidx.room',
    'androidx.security.crypto'
)) { $linkArgs += @('--extra-packages', $pkg) }
$linkArgs += $resZip
$linkArgs += $depResZips
& "$BT\aapt2.exe" @linkArgs
if ($LASTEXITCODE -ne 0) { Bad '资源链接失败（aapt2 link）' }
# ⚠️ 必须编译**全部** R.java，不能只取第一个。
#
# aapt2 link 会为本应用和每个 `--extra-packages` 声明的依赖包各生成一个 R.java
# （例如 `cn/whale/agent/device/R.java` 与 `androidx/lifecycle/runtime/R.java`）。
# 早先这里只编译了一个，导致：
#   - 应用自己的 R 类缺常量 → 真机 Resources$NotFoundException；
#   - 依赖库的 R 类干脆不存在 → 真机 ClassNotFoundException: …runtime.R$id。
# 两者在编译期与单测期都不报错，只有真机才暴露。
$rJavaList = @(Get-ChildItem $Gen -Recurse -Filter R.java)
if ($rJavaList.Count -eq 0) { Bad '未生成 R.java' }
& "$JDK\bin\javac.exe" -encoding UTF-8 -source 17 -target 17 -classpath $PLAT -nowarn `
    -d "$Out\classes" @($rJavaList.FullName)
if ($LASTEXITCODE -ne 0) { Bad 'R.java 编译失败' }
Ok "R 已生成并编译（$($rJavaList.Count) 个包）"
# ── 3. 编译全部 main 源 ────────────────────────────────────
# main 源文件列表：kapt 与正式编译都要用，所以在这里先算好。
# （早先只在 3/6 里定义，导致 kapt 步骤拿到 $null，编译器以为要进 REPL：
#   error: unable to run REPL, no scripting plugin loaded）
$mainFiles = (Get-ChildItem $Java -Filter *.kt).FullName
if (-not $mainFiles) { Bad '没有找到 main .kt 文件' }

# 注：这里曾经有一步 kapt（生成 Room 的 *_Impl）。
# Room 已整体移除（kapt 在手工 K2 工具链下生成不出 stub，
# 详见 DeviceStore.kt 顶部说明），持久化改为手写 SQLite，不再需要注解处理。

Step '3/6 编译 main Kotlin'
& "$JDK\bin\java.exe" "-Dfile.encoding=UTF-8" "-Dsun.jnu.encoding=UTF-8" -cp $kotlinClasspath `
    org.jetbrains.kotlin.cli.jvm.K2JVMCompiler `
    -no-stdlib -no-reflect `
    -cp "$stdlib;$PLAT;$libCp;$Out\classes" `
    "-Xplugin=$serPlugin" -jvm-target 17 -d "$Out\classes" @mainFiles
if ($LASTEXITCODE -ne 0) {
    Write-Host '  提示：若报错含乱码标识符，通常是源文件编码问题（需要 -Dfile.encoding=UTF-8）。' -ForegroundColor Yellow
    Bad "main Kotlin 编译失败（exit $LASTEXITCODE）"
}
$mainClasses = (Get-ChildItem "$Out\classes" -Recurse -Filter *.class).Count
Ok "main 编译通过：$($mainFiles.Count) 个源文件 / $mainClasses 个 class"
# ── 4. 编译测试源 ──────────────────────────────────────────
Step '4/6 编译 test Kotlin'
$testFiles = (Get-ChildItem $TestKt -Filter *.kt -ErrorAction SilentlyContinue).FullName
if (-not $testFiles) { Bad '没有找到 test .kt 文件' }
& "$JDK\bin\java.exe" "-Dfile.encoding=UTF-8" "-Dsun.jnu.encoding=UTF-8" -cp $kotlinClasspath `
    org.jetbrains.kotlin.cli.jvm.K2JVMCompiler `
    -no-stdlib -no-reflect `
    -cp "$stdlib;$PLAT;$libCp;$Out\classes" `
    "-Xplugin=$serPlugin" -jvm-target 17 -d "$Out\test-classes" @testFiles
if ($LASTEXITCODE -ne 0) { Bad "test Kotlin 编译失败（exit $LASTEXITCODE）" }
$testClasses = (Get-ChildItem "$Out\test-classes" -Recurse -Filter *.class).Count
Ok "test 编译通过：$($testFiles.Count) 个源文件 / $testClasses 个 class"
if ($SkipTests) {
    Write-Host "`n编译通过（已跳过测试）。" -ForegroundColor Green
    exit 0
}
# ── 5. JUnit4 真跑 ────────────────────────────────────────
Step '5/6 运行 Kotlin 单元测试'
$runnerCp = "$Out\classes;$Out\test-classes;$stdlib;$PLAT;$libCp"
$suites = Get-ChildItem "$Out\test-classes" -Recurse -Filter '*Test.class' |
    Where-Object { $_.Name -notmatch '\$' } |
    ForEach-Object {
        $rel = $_.FullName.Substring("$Out\test-classes\".Length) -replace '\\', '.' -replace '\.class$', ''
        $rel
    } | Sort-Object
$passed = 0
$failedSuites = @()
foreach ($suite in $suites) {
    $raw = & "$JDK\bin\java.exe" "-Dfile.encoding=UTF-8" -cp $runnerCp org.junit.runner.JUnitCore $suite 2>&1 | Out-String
    # 用 [regex]::Match 显式取捕获组：pwsh 7 的 `-match` 在捕获组上不保证写进 `$Matches[1]`
    # （实测 `$Matches.Count = 1`，只有 `$Matches[0]`，捕获组拿到空串，
    #  导致「26 个测试类通过，共 0 个用例」这种荒谬统计）。
    $okMatch = [regex]::Match($raw, 'OK \((\d+) tests\)')
    $runMatch = [regex]::Match($raw, 'Tests run: (\d+),')
    $shortName = ($suite -split '\.')[-1]
    if ($okMatch.Success) {
        $n = [int]$okMatch.Groups[1].Value
        $passed += $n
        Write-Host ("  {0,-26} OK   {1}" -f $shortName, $n) -ForegroundColor Green
    } elseif ($runMatch.Success) {
        $failedSuites += $suite
        Write-Host ("  {0,-26} FAIL {1}" -f $shortName, $runMatch.Groups[1].Value) -ForegroundColor Red
    } else {
        $failedSuites += $suite
        Write-Host ("  {0,-26} ERROR" -f $shortName) -ForegroundColor Red
    }
}
if ($failedSuites.Count -gt 0) { Bad "$($failedSuites.Count) 个测试类失败：$($failedSuites -join ', ')" }
Ok "全部 $($suites.Count) 个测试类通过，共 $passed 个用例"
# ── 6. 汇总 ───────────────────────────────────────────────
Step '6/6 结果'
@{
    jdk                  = (& "$JDK\bin\java.exe" -version 2>&1 | Select-Object -First 1) -replace '"', ''
    main_sources         = $mainFiles.Count
    main_classes         = $mainClasses
    test_sources         = $testFiles.Count
    test_classes         = $testClasses
    unit_tests_passed    = $passed
    test_suites          = $suites.Count
} | ConvertTo-Json | Tee-Object -FilePath (Join-Path $Work "last-run.json")
Write-Host "Kotlin 真编译 + 真跑测试通过。" -ForegroundColor Green
