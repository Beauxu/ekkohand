package cv.backpeng.ekkohand

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/**
 * 唯一界面：配对 + 两个权限引导 + 日志。
 * 全部代码构建布局，不引 AppCompat / Material，包体小、构建快。
 */
class MainActivity : Activity() {

    companion object {
        private const val REQ_CAPTURE = 1001
    }

    private lateinit var prefs: Prefs
    private lateinit var status: TextView
    private lateinit var logView: TextView
    private lateinit var nameEdit: EditText
    private lateinit var codeEdit: EditText

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildUi())
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 11)
        }
        if (prefs.paired) AgentService.start(this)
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    // ── UI ────────────────────────────────────────────────────
    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun buildUi(): ViewGroup {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(16))
        }

        root.addView(TextView(this).apply {
            text = "EkkoHand"
            textSize = 26f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "把这台手机交给工作台当手：截屏、点击、滑动、输入、开应用。\n" +
                "每次操作都要在 Ekko 会话里点「允许」。"
            textSize = 13f
            setPadding(0, dp(6), 0, dp(14))
        })

        status = TextView(this).apply {
            textSize = 15f
            setPadding(0, 0, 0, dp(14))
        }
        root.addView(status)

        root.addView(label("设备名"))
        nameEdit = EditText(this).apply {
            setText(prefs.deviceName.ifEmpty { Build.MODEL })
            inputType = InputType.TYPE_CLASS_TEXT
        }
        root.addView(nameEdit, matchWidth())

        root.addView(label("配对码（工作台生成，6 位数字）"))
        codeEdit = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            textSize = 20f
            gravity = Gravity.CENTER
            hint = "000000"
        }
        root.addView(codeEdit, matchWidth())

        root.addView(button("配对并连接") { pair() }, matchWidth(dp(8)))
        root.addView(button("授权屏幕截取") { requestCapture() }, matchWidth(dp(8)))
        root.addView(button("打开无障碍设置") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }, matchWidth(dp(8)))
        root.addView(button("断开并停止") {
            AgentService.stop(this)
            appendLog("已停止前台服务（配对信息保留）")
        }, matchWidth(dp(8)))

        logView = TextView(this).apply {
            textSize = 11f
            setTypeface(Typeface.MONOSPACE)
            setPadding(dp(8), dp(8), dp(8), dp(8))
            setBackgroundColor(Color.parseColor("#F4F4F2"))
        }
        val logScroll = ScrollView(this).apply {
            addView(logView)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f
            ).apply { topMargin = dp(14) }
        }
        root.addView(logScroll)

        return ScrollView(this).apply { addView(root) }
    }

    private fun matchWidth(top: Int = 0) = LinearLayout.LayoutParams(
        LinearLayout.LayoutParams.MATCH_PARENT,
        LinearLayout.LayoutParams.WRAP_CONTENT,
    ).apply { topMargin = dp(top) }

    private fun label(text: String) = TextView(this).apply {
        this.text = text
        textSize = 13f
        setPadding(0, dp(10), 0, dp(4))
    }

    private fun button(text: String, action: () -> Unit) = Button(this).apply {
        this.text = text
        setOnClickListener { action() }
    }

    // ── 动作 ──────────────────────────────────────────────────
    private fun pair() {
        val code = codeEdit.text.toString().filter { it.isDigit() }
        if (code.length != 6) {
            toast("配对码是 6 位数字，在工作台「设备」页生成")
            return
        }
        val name = nameEdit.text.toString().trim().ifEmpty { "Android 手机" }
        appendLog("正在配对…")
        Thread {
            try {
                val fp = Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID)
                    ?: Build.FINGERPRINT
                val res = HubClient.pair(
                    Prefs.DEFAULT_HUB, code, name, "android:$fp", Ops.caps(),
                )
                if (res.status != 200) {
                    val err = res.body.optString("error", "配对失败（HTTP ${res.status}）")
                    runOnUiThread { appendLog("配对失败：$err"); toast(err) }
                    return@Thread
                }
                prefs.save(
                    Prefs.DEFAULT_HUB, res.body.optString("device_id"),
                    res.body.optString("token"), name,
                )
                runOnUiThread {
                    codeEdit.setText("")
                    appendLog("配对成功：${prefs.deviceId}")
                    refreshStatus()
                    AgentService.start(this)
                    toast("配对成功")
                }
            } catch (e: Exception) {
                runOnUiThread {
                    appendLog("配对失败：${e.javaClass.simpleName}: ${e.message}")
                    toast("连不上服务器，检查网络")
                }
            }
        }.start()
    }

    private fun requestCapture() {
        val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        startActivityForResult(mpm.createScreenCaptureIntent(), REQ_CAPTURE)
        appendLog("系统会弹「开始录制或共享屏幕」——选「开始」，这是截图的唯一合法入口")
    }

    @Deprecated("Activity 结果回调；API 34 仍可用")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_CAPTURE) return
        if (resultCode != RESULT_OK || data == null) {
            appendLog("截屏授权被拒绝")
            return
        }
        AgentService.start(this)
        AgentService.bindCapture(this, resultCode, data)
        appendLog("截屏授权已提交给服务；通知栏显示「截屏已就绪」即可")
    }

    private fun refreshStatus() {
        val parts = ArrayList<String>()
        parts.add(if (prefs.paired) "已配对 · ${prefs.deviceName.ifEmpty { "本机" }}" else "未配对")
        parts.add(if (EkkoAccessibilityService.connected()) "无障碍 ✅" else "无障碍 ❌")
        parts.add(if (ScreenCapture.isReady()) "截屏 ✅" else "截屏 ❌")
        status.text = parts.joinToString("   ")
        status.setTextColor(
            if (prefs.paired && EkkoAccessibilityService.connected()) Color.parseColor("#1A7F37")
            else Color.parseColor("#9A6700")
        )
    }

    private fun appendLog(line: String) {
        val stamp = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())
            .format(java.util.Date())
        val merged = logView.text.toString()
        val next = (if (merged.isEmpty()) "" else merged + "\n") + "$stamp  $line"
        logView.text = next.takeLast(6000)
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
