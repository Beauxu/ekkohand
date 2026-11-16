package cv.backpeng.ekkohand

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 前台服务：长轮询 hub、执行任务。
 *
 * 生命周期是「用户可见」的：通知栏常驻一条通知，点通知能回到 App；
 * 用户在系统里划掉/强停就真停，不做任何隐藏复活。
 */
class AgentService : Service() {

    companion object {
        private const val TAG = "EkkoHand"
        const val ACTION_START = "cv.backpeng.ekkohand.START"
        const val ACTION_STOP = "cv.backpeng.ekkohand.STOP"
        const val ACTION_CAPTURE = "cv.backpeng.ekkohand.CAPTURE"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        private const val CHANNEL = "ekkohand"
        private const val NOTIF_ID = 1

        fun start(ctx: Context) {
            val i = Intent(ctx, AgentService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i)
            else ctx.startService(i)
        }

        fun stop(ctx: Context) {
            ctx.startService(Intent(ctx, AgentService::class.java).setAction(ACTION_STOP))
        }

        fun bindCapture(ctx: Context, code: Int, data: Intent) {
            val i = Intent(ctx, AgentService::class.java).setAction(ACTION_CAPTURE)
                .putExtra(EXTRA_CODE, code)
                .putExtra(EXTRA_DATA, data)
            ctx.startService(i)
        }
    }

    private lateinit var prefs: Prefs
    private val running = AtomicBoolean(false)
    private var worker: Thread? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopLoop()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_CAPTURE -> {
                ensureForeground("正在准备截屏…")
                @Suppress("DEPRECATION")
                val data = intent.getParcelableExtra(EXTRA_DATA) as? Intent
                bindScreenCapture(intent.getIntExtra(EXTRA_CODE, 0), data)
            }
            else -> {
                ensureForeground("正在连接工作台…")
                startLoop()
            }
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        stopLoop()
        super.onDestroy()
    }

    // ── 前台通知 ──────────────────────────────────────────────
    private fun createChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, "EkkoHand 运行状态", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("EkkoHand")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    private fun ensureForeground(text: String) {
        val n = buildNotification(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(text))
    }

    /** 切到 mediaProjection 类型后再 getMediaProjection（Android 14 的硬性顺序）。 */
    private fun bindScreenCapture(code: Int, data: Intent?) {
        if (data == null) {
            updateNotification("截屏授权失败：没有拿到授权数据")
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIF_ID, buildNotification("正在准备截屏…"),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION,
            )
        }
        val ok = runCatching { ScreenCapture.attach(this, code, data) }.getOrDefault(false)
        updateNotification(if (ok) "截屏已就绪 · 在线" else "截屏授权失败，请回 App 重新授权")
    }

    // ── 轮询 ──────────────────────────────────────────────────
    private fun startLoop() {
        if (!running.compareAndSet(false, true)) return
        worker = Thread({ loop() }, "ekkohand-poll").also { it.start() }
    }

    private fun stopLoop() {
        running.set(false)
        worker?.interrupt()
        worker = null
    }

    private fun loop() {
        if (!prefs.paired) {
            updateNotification("尚未配对：打开 App 输入 6 位配对码")
            running.set(false)
            return
        }
        var backoff = 2_000L
        var lastNote = ""
        while (running.get()) {
            try {
                val res = HubClient.post(
                    prefs.hub, "/poll",
                    JSONObject()
                        .put("device_id", prefs.deviceId)
                        .put("token", prefs.token)
                        .put("wait", 25)
                        .put("caps", JSONArray(Ops.caps()))
                        .put("version", Prefs.VERSION),
                    40_000,
                )
                backoff = 2_000L
                when {
                    res.status == 204 -> {
                        if (lastNote != "online") {
                            lastNote = "online"
                            updateNotification("在线 · ${prefs.deviceName.ifEmpty { "Android" }}")
                        }
                    }
                    res.status == 401 -> {
                        updateNotification("令牌失效，请在 App 里重新配对")
                        running.set(false)
                        return
                    }
                    res.status != 200 -> {
                        updateNotification("服务端异常 ${res.status}，稍后重试")
                        Thread.sleep(3_000)
                    }
                    else -> {
                        val taskId = res.body.optString("task_id")
                        val method = res.body.optString("method")
                        val params = res.body.optJSONObject("params") ?: JSONObject()
                        updateNotification("执行中：$method")
                        val (ok, payload) = Ops.handle(this, method, params)
                        HubClient.post(
                            prefs.hub, "/result",
                            JSONObject()
                                .put("device_id", prefs.deviceId)
                                .put("token", prefs.token)
                                .put("task_id", taskId)
                                .put("ok", ok)
                                .put("result", if (ok) payload else JSONObject())
                                .put(
                                    "error",
                                    if (ok) JSONObject.NULL else payload.optString("error", "失败"),
                                ),
                            30_000,
                        )
                        lastNote = ""
                    }
                }
            } catch (e: InterruptedException) {
                return
            } catch (e: Exception) {
                if (!running.get()) return
                Log.w(TAG, "poll 失败", e)
                updateNotification("连接断开，${backoff / 1000}s 后重试")
                try {
                    Thread.sleep(backoff)
                } catch (ie: InterruptedException) {
                    return
                }
                backoff = minOf(backoff * 2, 30_000L)
            }
        }
    }
}
