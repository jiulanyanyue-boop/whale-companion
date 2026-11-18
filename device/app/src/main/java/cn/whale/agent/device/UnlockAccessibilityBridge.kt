package cn.whale.agent.device

import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo

/** Local credential input is restricted to the OS keyguard; trees never leave this class. */
object UnlockAccessibilityBridge {
    enum class Result { WAITING, SUBMITTED, PARTIAL_FAILURE }
    @Volatile private var service: WhaleAccessibilityService? = null
    fun attach(value: WhaleAccessibilityService) { service = value }
    fun detach(value: WhaleAccessibilityService) { if (service === value) service = null }

    private fun roots(): List<AccessibilityNodeInfo> {
        val current = service ?: return emptyList()
        return buildList {
            current.rootInActiveWindow?.let(::add)
            current.windows.mapNotNull { it.root }.forEach(::add)
        }.distinctBy { it.windowId }.filter { it.packageName?.toString() == "com.android.systemui" }
    }

    fun fill(credential: UnlockCredentialStore.Credential): Result {
        for (root in roots()) {
            val nodes = flatten(root)
            // A PIN keypad has all ten digit controls, not a generic editable app field.
            val digits = nodes.mapNotNull { node ->
                val id = node.viewIdResourceName.orEmpty().substringAfterLast('/')
                val digit = id.removePrefix("key").takeIf { id.startsWith("key") && it.length == 1 && it[0] in '0'..'9' }
                if (digit != null && node.isVisibleToUser && node.isClickable) digit[0] to node else null
            }.toMap()
            if (credential.type == UnlockCredentialStore.Type.PIN && digits.size == 10) {
                // Never append to a PIN that someone has already started typing.
                val field = nodes.firstOrNull { it.viewIdResourceName.orEmpty().substringAfterLast('/') in setOf("fixedPinEntry", "pinEntry") }
                if (!field?.text.isNullOrEmpty()) return Result.PARTIAL_FAILURE
                for (digit in credential.value) {
                    if (!digits.getValue(digit).performAction(AccessibilityNodeInfo.ACTION_CLICK)) return Result.PARTIAL_FAILURE
                }
                submit(nodes)
                return Result.SUBMITTED
            }
            if (credential.type == UnlockCredentialStore.Type.PASSWORD) {
                val field = nodes.firstOrNull { it.isEditable && it.isPassword && it.isVisibleToUser } ?: continue
                val args = Bundle().apply {
                    putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, credential.value)
                }
                if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) return Result.PARTIAL_FAILURE
                // Android 11+ supports the editor's real IME action.
                val ime = if (android.os.Build.VERSION.SDK_INT >= 30) field.actionList.firstOrNull {
                    it.id == AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id
                } else null
                if (ime != null) field.performAction(ime.id) else submit(nodes)
                return Result.SUBMITTED
            }
        }
        return Result.WAITING
    }

    private fun submit(nodes: List<AccessibilityNodeInfo>) {
        nodes.firstOrNull { it.isVisibleToUser && it.isClickable &&
            it.viewIdResourceName.orEmpty().substringAfterLast('/') in setOf("key_enter", "keyguard_enter", "keyguard_ok_button")
        }?.performAction(AccessibilityNodeInfo.ACTION_CLICK)
    }

    private fun flatten(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val result = mutableListOf<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 40 || result.size >= 1200) return
            result.add(node)
            for (i in 0 until node.childCount) node.getChild(i)?.let { visit(it, depth + 1) }
        }
        visit(root, 0)
        return result
    }
}
