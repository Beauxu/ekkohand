package cv.backpeng.ekkohand

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.graphics.Path
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * 无障碍服务：点击、滑动、返回/主页、往输入框写文字。
 *
 * 注意它的边界（Android 的安全设计，不是实现偷懒）：
 * - 密码框、部分银行/游戏控件会声明 importantForAccessibility=no 或拒绝 SET_TEXT；
 * - 需要用户手动在系统设置里开启，且系统随时可能关掉。
 */
class EkkoAccessibilityService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: EkkoAccessibilityService? = null
            private set

        fun connected(): Boolean = instance != null
    }

    override fun onServiceConnected() {
        instance = this
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit

    override fun onInterrupt() = Unit

    fun tap(x: Float, y: Float): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 60))
            .build()
        return dispatchGesture(gesture, null, null)
    }

    fun swipe(x1: Float, y1: Float, x2: Float, y2: Float, durationMs: Long): Boolean {
        val path = Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs))
            .build()
        return dispatchGesture(gesture, null, null)
    }

    /** 成功返回 null，失败返回原因。 */
    fun inputText(text: String): String? {
        val root = rootInActiveWindow ?: return "拿不到当前窗口（应用可能禁止无障碍读取）"
        val node = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: return "没有聚焦的输入框：先点一下要输入的框"
        if (!node.isEditable) return "当前焦点不是可编辑控件"
        val args = Bundle().apply {
            putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text,
            )
        }
        if (node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) return null
        // 回退：剪贴板 + 粘贴（有些自定义输入框只认 PASTE）
        val cm = getSystemService(ClipboardManager::class.java)
        cm.setPrimaryClip(ClipData.newPlainText("ekkohand", text))
        node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        return if (node.performAction(AccessibilityNodeInfo.ACTION_PASTE)) null
        else "这个输入框拒绝写入（安全键盘/自定义控件）"
    }

    fun global(action: Int): Boolean = performGlobalAction(action)
}
