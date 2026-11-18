package cn.whale.agent.device

import android.Manifest
import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.content.ContextCompat

/**
 * M11 首次配置向导与 OEM 引导页（Android 侧薄包装）。
 *
 * 规范来源：
 * - `docs/modules/M11-oem-permissions-product.md`「首次配置向导」「权限原则」
 * - `docs/modules/M11-oem-permissions-development.md`「固定适配层」「后台策略」
 *
 * 本 Activity 只做三件事：
 * 1. **只读**探测真实权限状态（[AndroidPermissionProbe] / [AndroidBatteryOptimizationReader]）；
 * 2. 把判定交给纯逻辑 [PermissionState] 与 [OemProfile]，本页不含任何品牌分支；
 * 3. 给用户「申请该项」或「打开系统设置页」两个显式入口。
 *
 * 固定禁止（违反即返工）：
 * - 不用无障碍点击系统设置页做「自动授权」；
 * - 不合并成一个「全部允许」的批量弹窗（相机/录音/联系人/短信逐项申请）；
 * - 不静默修改系统设置（电池优化只跳转 + 说明）；
 * - 不显示「在线」假状态：读不到的状态一律 `BLOCKED_BY_OEM`；
 * - 沙箱/Intent 失败不阻塞普通聊天：本页任何失败只更新本页文案，不抛出到上层。
 *
 * 模式与白名单（第 6 步）由 M01 的 [MainActivity] 承载，本页只给入口，不复制模式逻辑。
 */
class PermissionGuideActivity : Activity() {

    private lateinit var profile: OemProfile
    private lateinit var probe: AndroidPermissionProbe
    private lateinit var content: LinearLayout

    /** 已请求过的运行时权限，用于 `onRequestPermissionsResult` 回填文案。 */
    private val pendingRequestKind = HashMap<Int, PermissionKind>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WhaleUi.setup(this)
        profile = OemProfile.identify(
            manufacturer = Build.MANUFACTURER,
            brand = Build.BRAND,
            sdkInt = Build.VERSION.SDK_INT,
        )
        probe = AndroidPermissionProbe(this)
        setContentView(buildLayout())
        render()
    }

    override fun onResume() {
        super.onResume()
        // 用户可能刚在系统设置页里改过状态；必须重新读取，不能沿用旧缓存。
        if (::content.isInitialized) render()
    }

    private fun buildLayout(): ScrollView {
        val root = WhaleUi.page(this)
        root.addView(WhaleUi.button(this, "返回首页") { finish() })
        root.addView(WhaleUi.title(this, "权限与准备"))
        root.addView(WhaleUi.text(this, "按需要开启权限，每一项都由你决定。"))
        root.addView(WhaleUi.text(this, profile.displayName, 12f))
        content = WhaleUi.card(this)
        root.addView(content)
        root.addView(WhaleUi.text(this, "系统设置需要你手动确认。我们不会自动授权，也不会在后台持续录音。", 12f))
        return WhaleUi.scroll(this, root)
    }

    /** 重新渲染全部 6 步；顺序固定，直接来自 [PermissionState.firstRunWizard]。 */
    private fun render() {
        content.removeAllViews()
        val features = PermissionFeaturePolicy.GUIDE
        PermissionState.firstRunWizard.forEach { step ->
            content.addView(TextView(this).apply {
                text = "${step.index}. ${step.title}"
                textSize = 16f
                setPadding(0, 18, 0, 6)
            })
            when (step.key) {
                // 第 5 步的后台引导由 [renderBackgroundSection] 统一渲染，避免同一项渲染两遍。
                WizardStepKey.BACKGROUND -> renderBackgroundSection()
                // 第 6 步：只给模式页入口，不复制 M01 的模式切换逻辑。
                WizardStepKey.MODE_AND_ALLOWLIST -> renderModeAndAllowlistStep()
                // 其余步骤逐项渲染，逐项给按钮：绝不合并成「全部允许」。
                else -> step.kinds.forEach { kind -> renderKind(kind, features) }
            }
        }
        WhaleUi.polish(content)
    }

    private fun renderKind(kind: PermissionKind, features: PermissionFeaturePolicy) {
        val verdict = PermissionState.evaluate(kind, probe.read(kind), profile, features)
        content.addView(TextView(this).apply {
            text = verdict.displayText
            textSize = 13f
            setPadding(0, 4, 0, 2)
        })
        if (verdict.note.isNotBlank()) {
            content.addView(TextView(this).apply {
                text = verdict.note
                textSize = 11f
                setPadding(0, 0, 0, 2)
            })
        }
        // 「当前不需要」的项不给按钮，避免用户误以为还要申请；
        // 其余项（含已授权）都给可复核的入口，界面始终反映真实状态。
        if (verdict.status == PermissionStatus.NOT_APPLICABLE) return
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(-1, -2)
            gravity = Gravity.CENTER_VERTICAL
        }
        if (verdict.canRequestInApp) {
            row.addView(Button(this).apply {
                text = getString(R.string.m11_action_request)
                setOnClickListener { requestKind(kind) }
            })
        }
        if (verdict.canOpenSettings) {
            row.addView(Button(this).apply {
                text = getString(R.string.m11_action_open_settings)
                setOnClickListener { openSettings(kind) }
            })
        }
        if (row.childCount > 0) content.addView(row)
    }

    /** 第 6 步：模式和白名单。只给入口，不复制 M01 的模式切换逻辑。 */
    private fun renderModeAndAllowlistStep() {
        val mode = AgentModeStore(this).get()
        content.addView(TextView(this).apply {
            text = "当前模式：" + (if (mode.isLocal) "本地桌宠（LOCAL_PET）" else "远程控制（REMOTE_B）")
            textSize = 13f
            setPadding(0, 4, 0, 4)
        })
        content.addView(TextView(this).apply {
            text = getString(R.string.m11_mode_allowlist_note)
            textSize = 11f
            setPadding(0, 0, 0, 4)
        })
        content.addView(Button(this).apply {
            text = getString(R.string.m11_action_open_mode_page)
            setOnClickListener {
                // 模式切换仍然只能在 M01 设置页里点，本页不提供切换控件。
                runCatching { startActivity(Intent(this@PermissionGuideActivity, MainActivity::class.java)) }
                    .onFailure { logLaunchFailure("MainActivity", it) }
            }
        })
    }

    /** 第 5 步的后台引导：电池优化只作可选说明，绝不静默修改系统设置。 */
    private fun renderBackgroundSection() {
        val battery = AndroidBatteryOptimizationReader.read(this)
        val plan = BackgroundKeepAliveGuide.plan(profile, battery)
        plan.steps.forEach { step ->
            content.addView(TextView(this).apply {
                text = step.displayText
                textSize = 13f
                setPadding(0, 4, 0, 2)
            })
            if (step.note.isNotBlank()) {
                content.addView(TextView(this).apply {
                    text = step.note
                    textSize = 11f
                    setPadding(0, 0, 0, 2)
                })
            }
            if (step.settingsIntents.isNotEmpty() &&
                step.status != PermissionStatus.GRANTED &&
                step.status != PermissionStatus.NOT_APPLICABLE
            ) {
                content.addView(Button(this).apply {
                    text = getString(R.string.m11_action_open_settings)
                    setOnClickListener { openBackgroundSettings(step) }
                })
            }
        }
        content.addView(TextView(this).apply {
            text = getString(R.string.m11_battery_optional_note)
            textSize = 11f
            setPadding(0, 6, 0, 0)
        })
    }

    /**
     * 逐项申请运行时权限。一次只申请一项，`requestCode` 与 [PermissionKind.ordinal] 绑定。
     *
     * 通知在 API 33 以下没有 `POST_NOTIFICATIONS`，此时 [PermissionKind.isRuntimeRequestable]
     * 返回 false，本方法不会被调用，用户只能走设置页。
     */
    private fun requestKind(kind: PermissionKind) {
        val permission = kind.manifestPermission ?: return
        if (!kind.isRuntimeRequestable(Build.VERSION.SDK_INT)) return
        pendingRequestKind[kind.ordinal] = kind
        runCatching { requestPermissions(arrayOf(permission), kind.ordinal) }
            .onFailure { logLaunchFailure("requestPermissions:${kind.name}", it) }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val kind = pendingRequestKind.remove(requestCode) ?: return
        val granted = grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED }
        // 只刷新本页文案；拒绝时不反复弹窗，也不伪装成已授权。
        Log.i(LOG_TAG, "runtime permission result kind=${kind.name} granted=$granted")
        render()
    }

    /**
     * 打开该权限项的设置页：先试标准候选，再试厂商候选（未验收），全部失败才降级为
     * `BLOCKED_BY_OEM` + 手动路径。
     */
    private fun openSettings(kind: PermissionKind) {
        val intents = profile.settingsIntents(kind)
        if (launchFirstWorking(intents)) {
            render()
            return
        }
        val verdict = PermissionState.onSettingsLaunchFailed(kind, profile)
        Log.w(LOG_TAG, "settings launch failed kind=${kind.name} manual=${verdict.manualPath}")
        showBlockedByOem(verdict)
        render()
    }

    private fun openBackgroundSettings(step: BackgroundKeepAliveStep) {
        if (launchFirstWorking(step.settingsIntents)) {
            render()
            return
        }
        val kind = when (step.action) {
            BackgroundKeepAliveAction.BATTERY_OPTIMIZATION_WHITELIST -> PermissionKind.BATTERY_OPTIMIZATION
            BackgroundKeepAliveAction.OEM_AUTO_START -> PermissionKind.AUTO_START
            else -> PermissionKind.BACKGROUND_RUN
        }
        val verdict = PermissionState.onSettingsLaunchFailed(kind, profile)
        Log.w(LOG_TAG, "background settings launch failed action=${step.action.name} manual=${verdict.manualPath}")
        showBlockedByOem(verdict)
        render()
    }

    /**
     * 依次尝试候选 Intent，第一个能打开的就返回 true。
     *
     * 固定顺序：标准 AOSP 候选 → 厂商候选（[OemIntentVerification.VENDOR_UNVERIFIED]）。
     * 未识别厂商没有厂商候选，因此这里不会猜测任何组件。
     */
    private fun launchFirstWorking(intents: List<OemSettingsIntent>): Boolean {
        for (candidate in intents) {
            val intent = buildIntent(candidate) ?: continue
            // 显式组件必须先确认可解析，避免抛出后影响聊天以外的流程。
            if (intent.component != null && intent.resolveActivity(packageManager) == null) {
                Log.w(LOG_TAG, "vendor component not resolvable: ${candidate.componentPackage}/${candidate.componentClass}")
                continue
            }
            val launched = runCatching { startActivity(intent); true }.getOrDefault(false)
            if (launched) return true
            Log.w(LOG_TAG, "settings intent rejected: ${candidate.label}")
        }
        return false
    }

    private fun buildIntent(candidate: OemSettingsIntent): Intent? {
        val action = candidate.action
        val component = if (candidate.isExplicitComponent) {
            ComponentName(candidate.componentPackage!!, candidate.componentClass!!)
        } else {
            null
        }
        if (action == null && component == null) return null
        val intent = Intent()
        if (action != null) intent.action = action
        if (component != null) intent.component = component
        if (candidate.requiresPackageUri) intent.data = Uri.parse("package:$packageName")
        if (candidate.requiresAppPackageExtra) intent.putExtra(OemProfile.EXTRA_APP_PACKAGE, packageName)
        // 系统设置页一律不加 FLAG_ACTIVITY_NEW_TASK：必须由本前台 Activity 显式启动。
        return intent
    }

    /** `BLOCKED_BY_OEM` 的展示：只说明手动路径，不提供任何「自动打开/自动授权」按钮。 */
    private fun showBlockedByOem(verdict: PermissionVerdict) {
        android.app.AlertDialog.Builder(this)
            .setTitle(getString(R.string.m11_blocked_title))
            .setMessage(
                buildString {
                    append(verdict.kind.displayName)
                    append("：")
                    append(verdict.status.label)
                    append("\n\n请按下面路径手动操作：\n")
                    append(verdict.manualPath)
                    append("\n\n不同品牌与系统版本的菜单名称可能略有差异。")
                },
            )
            .setPositiveButton(getString(R.string.m11_blocked_ack), null)
            .show()
    }

    private fun logLaunchFailure(what: String, error: Throwable) {
        // 失败只记本地日志，不抛出；设置页打不开也不能阻塞普通聊天。
        Log.w(LOG_TAG, "launch failed: $what", error)
    }

    private companion object {
        const val LOG_TAG = "WhaleDeviceM11"
    }
}

/**
 * Android 只读权限探测。
 *
 * 每一项都对应 [PermissionKind]，并且**只读**：不申请、不开启、不修改任何系统设置。
 * 判定本身不在这里做——这里只把「真实读数」交给纯逻辑 [PermissionState.evaluate]。
 */
class AndroidPermissionProbe(private val context: Context) : PermissionProbe {

    override fun read(kind: PermissionKind): ProbeResult = when (kind) {
        PermissionKind.ACCESSIBILITY_SERVICE -> ProbeResult(isAccessibilityEnabled())
        PermissionKind.OVERLAY -> ProbeResult(Settings.canDrawOverlays(context))
        PermissionKind.NOTIFICATIONS -> readNotifications()
        PermissionKind.CAMERA -> checkRuntimePermission(Manifest.permission.CAMERA)
        PermissionKind.RECORD_AUDIO -> checkRuntimePermission(Manifest.permission.RECORD_AUDIO)
        PermissionKind.READ_CONTACTS -> checkRuntimePermission(Manifest.permission.READ_CONTACTS)
        PermissionKind.SEND_SMS -> checkRuntimePermission(Manifest.permission.SEND_SMS)
        // 相册走系统 Photo Picker：本应用不持有、也不申请存储权限。
        PermissionKind.PHOTO_PICKER -> ProbeResult(
            granted = false,
            readable = true,
            note = "相册由系统 Photo Picker 处理，不需要存储权限",
        )
        PermissionKind.BATTERY_OPTIMIZATION -> AndroidBatteryOptimizationReader.read(context).let { battery ->
            ProbeResult(granted = battery.ignoringBatteryOptimizations, readable = battery.readable, note = battery.note)
        }
        // 厂商自启动/后台运行没有可读的权威 API：标记不可读，由 OemProfile 给手动路径。
        PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> ProbeResult.unreadable(
            "系统没有公开读取厂商自启动/后台运行开关的 API，只能由用户手动确认",
        )
    }

    /**
     * 与 M01 [MainActivity] 完全一致的只读判定：
     * `Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES` 里是否包含本服务组件。
     *
     * 只读探测，不主动开启任何服务；也不通过无障碍点击设置页做「自动授权」。
     */
    private fun isAccessibilityEnabled(): Boolean {
        val component = ComponentName(context, WhaleAccessibilityService::class.java)
        val enabled = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ) ?: return false
        return enabled.split(':').any { flat -> ComponentName.unflattenFromString(flat) == component }
    }

    private fun readNotifications(): ProbeResult {
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? android.app.NotificationManager
            ?: return ProbeResult.unreadable("无法获取通知服务")
        // `areNotificationsEnabled` 从 API 24 起可用；本应用 minSdk=26。
        return ProbeResult(manager.areNotificationsEnabled())
    }

    private fun checkRuntimePermission(permission: String): ProbeResult = ProbeResult(
        granted = ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED,
    )
}

/**
 * 电池优化状态的只读读取器。
 *
 * 只读：`PowerManager.isIgnoringBatteryOptimizations`。本模块**没有**任何
 * `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` 之类的写入口 —— 电池优化仅作可选引导，
 * 不能静默修改系统设置。
 */
object AndroidBatteryOptimizationReader {
    fun read(context: Context): BatteryOptimizationProbe {
        if (Build.VERSION.SDK_INT < 23) {
            return BatteryOptimizationProbe(
                ignoringBatteryOptimizations = false,
                readable = false,
                note = "API 23 以下没有电池优化白名单",
            )
        }
        val power = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
            ?: return BatteryOptimizationProbe(false, readable = false, note = "无法获取电源服务")
        return runCatching {
            BatteryOptimizationProbe(power.isIgnoringBatteryOptimizations(context.packageName))
        }.getOrElse {
            BatteryOptimizationProbe(false, readable = false, note = "当前系统不允许读取电池优化状态")
        }
    }
}
