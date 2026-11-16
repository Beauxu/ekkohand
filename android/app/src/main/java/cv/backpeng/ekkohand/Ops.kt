package cv.backpeng.ekkohand

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.content.Intent
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 方法实现。字段名与桌面端 agent.py 保持一致：
 * screenshot / input.* / app.* / fs.*。
 *
 * Android 端明确不做：script.run（没有 root，不为这个要 root）、fs.delete/move
 * （分区存储下误删风险不对等，先只读写在 app 私有目录）。
 */
object Ops {

    private const val FS_READ_LIMIT = 400_000

    fun caps(): List<String> = listOf(
        "screenshot",
        "input.click", "input.type", "input.key", "input.drag", "input.scroll",
        "app.list", "app.launch",
        "fs.list", "fs.read", "fs.write",
    )

    fun handle(ctx: Context, method: String, p: JSONObject): Pair<Boolean, JSONObject> = try {
        val result: JSONObject = when (method) {
            "screenshot" -> screenshot(p)
            "input.click" -> click(p)
            "input.type" -> typeText(p)
            "input.key" -> key(p)
            "input.drag" -> drag(p)
            "input.scroll" -> scroll(p)
            "app.list" -> appList(ctx, p)
            "app.launch" -> appLaunch(ctx, p)
            "fs.list" -> fsList(ctx, p)
            "fs.read" -> fsRead(ctx, p)
            "fs.write" -> fsWrite(ctx, p)
            "script.run" -> throw UnsupportedOperationException(
                "Android 端不能跑 shell（没有 root）。要执行命令请在电脑或服务器上做。"
            )
            else -> throw UnsupportedOperationException("设备不支持的方法：$method")
        }
        Pair(true, result)
    } catch (e: Exception) {
        Pair(false, JSONObject().put("error", "${e.javaClass.simpleName}: ${e.message ?: ""}"))
    }

    // ── 截图 ──────────────────────────────────────────────────
    private fun screenshot(p: JSONObject): JSONObject {
        if (!ScreenCapture.isReady()) {
            throw IllegalStateException(
                "截屏授权已失效：打开 EkkoHand App，点「授权屏幕截取」再试（手机重启后必须重新授权）"
            )
        }
        val maxEdge = p.optInt("max_edge", 0)
        val shot = ScreenCapture.capturePng(maxEdge)
            ?: throw IllegalStateException("没取到画面（屏幕可能正被安全界面遮挡，如支付/密码页）")
        val (bytes, w, h) = shot
        return JSONObject()
            .put("png_b64", Base64.encodeToString(bytes, Base64.NO_WRAP))
            .put("width", w)
            .put("height", h)
            .put("bytes", bytes.size)
            .put("scaled", maxEdge > 0)
    }

    // ── 输入 ──────────────────────────────────────────────────
    private fun a11y(): EkkoAccessibilityService =
        EkkoAccessibilityService.instance
            ?: throw IllegalStateException(
                "无障碍服务没开：设置 → 无障碍 → 已下载的服务 → EkkoHand 操作 → 开启"
            )

    private fun click(p: JSONObject): JSONObject {
        val x = p.getDouble("x").toFloat()
        val y = p.getDouble("y").toFloat()
        if (!a11y().tap(x, y)) throw IllegalStateException("手势被系统拒绝")
        return JSONObject().put("x", x).put("y", y).put("backend", "accessibility")
    }

    private fun drag(p: JSONObject): JSONObject {
        val x1 = p.getDouble("x1").toFloat()
        val y1 = p.getDouble("y1").toFloat()
        val x2 = p.getDouble("x2").toFloat()
        val y2 = p.getDouble("y2").toFloat()
        val steps = p.optInt("steps", 12).coerceIn(2, 60)
        if (!a11y().swipe(x1, y1, x2, y2, (steps * 40).toLong())) {
            throw IllegalStateException("手势被系统拒绝")
        }
        return JSONObject().put("x1", x1).put("y1", y1).put("x2", x2).put("y2", y2)
    }

    private fun scroll(p: JSONObject): JSONObject {
        val amount = p.optInt("amount", 0)
        if (amount == 0) throw IllegalArgumentException("amount 不能为 0（正数向下、负数向上）")
        val svc = a11y()
        val metrics = svc.resources.displayMetrics
        val w = metrics.widthPixels.toFloat()
        val h = metrics.heightPixels.toFloat()
        val cx = w / 2f
        // 正数 = 内容向下滚动 = 手指从下往上滑
        val travel = (h * (0.12f + 0.03f * kotlin.math.abs(amount).coerceAtMost(10))).coerceAtMost(h * 0.45f)
        val (from, to) = if (amount > 0) Pair(h * 0.62f, h * 0.62f - travel)
        else Pair(h * 0.38f, h * 0.38f + travel)
        if (!svc.swipe(cx, from, cx, to, 260)) throw IllegalStateException("手势被系统拒绝")
        return JSONObject().put("amount", amount).put("unit", "line").put("backend", "accessibility")
    }

    private fun typeText(p: JSONObject): JSONObject {
        val text = p.optString("text", "")
        if (text.isEmpty()) throw IllegalArgumentException("text 为空")
        val err = a11y().inputText(text)
        if (err != null) throw IllegalStateException(err)
        return JSONObject().put("typed", text.length).put("backend", "accessibility")
    }

    private fun key(p: JSONObject): JSONObject {
        val keys = p.optString("keys", "").trim().lowercase()
        val action = when (keys) {
            "back", "escape", "esc" -> AccessibilityService.GLOBAL_ACTION_BACK
            "home" -> AccessibilityService.GLOBAL_ACTION_HOME
            "recents", "appswitch" -> AccessibilityService.GLOBAL_ACTION_RECENTS
            "notifications", "notification" -> AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS
            "quicksettings" -> AccessibilityService.GLOBAL_ACTION_QUICK_SETTINGS
            "lock" -> AccessibilityService.GLOBAL_ACTION_LOCK_SCREEN
            else -> throw UnsupportedOperationException(
                "Android 只支持 back / home / recents / notifications / quicksettings / lock"
            )
        }
        if (!a11y().global(action)) throw IllegalStateException("系统拒绝了该操作")
        return JSONObject().put("keys", keys).put("backend", "accessibility")
    }

    // ── 应用 ──────────────────────────────────────────────────
    private fun appList(ctx: Context, p: JSONObject): JSONObject {
        val query = p.optString("query", "").lowercase()
        val limit = p.optInt("limit", 200).coerceIn(1, 500)
        val pm = ctx.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val seen = HashSet<String>()
        val rows = JSONArray()
        for (info in pm.queryIntentActivities(intent, 0)) {
            val pkg = info.activityInfo.packageName
            if (!seen.add(pkg)) continue
            val label = info.loadLabel(pm).toString()
            if (query.isNotEmpty() && !label.lowercase().contains(query) &&
                !pkg.lowercase().contains(query)
            ) continue
            rows.put(JSONObject().put("name", label).put("path", pkg).put("folder", "launcher"))
        }
        val total = rows.length()
        val sliced = JSONArray()
        for (i in 0 until minOf(total, limit)) sliced.put(rows.get(i))
        return JSONObject().put("apps", sliced).put("total", total).put("truncated", total > limit)
    }

    private fun appLaunch(ctx: Context, p: JSONObject): JSONObject {
        val name = p.optString("name", "").trim()
        val path = p.optString("path", "").trim()
        val pm = ctx.packageManager
        var pkg = path
        if (pkg.isEmpty() && name.isNotEmpty()) {
            val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
            for (info in pm.queryIntentActivities(intent, 0)) {
                if (info.loadLabel(pm).toString().equals(name, ignoreCase = true)) {
                    pkg = info.activityInfo.packageName
                    break
                }
            }
        }
        if (pkg.isEmpty()) throw IllegalArgumentException("没找到这个应用：${name.ifEmpty { path }}")
        val launch = pm.getLaunchIntentForPackage(pkg)
            ?: throw IllegalArgumentException("$pkg 没有可启动的界面")
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(launch)
        return JSONObject().put("launched", pkg).put("backend", "packageManager")
    }

    // ── 文件（仅 app 私有目录）────────────────────────────────
    private fun resolve(ctx: Context, raw: String?): File {
        val base = ctx.filesDir.canonicalFile
        var s = (raw ?: "~").trim()
        if (s.isEmpty()) s = "~"
        val target = when {
            s == "~" -> base
            s.startsWith("~/") -> File(base, s.substring(2))
            else -> File(s)
        }.canonicalFile
        val ext = ctx.getExternalFilesDir(null)?.canonicalFile
        val insideBase = target.path == base.path || target.path.startsWith(base.path + File.separator)
        val insideExt = ext != null &&
            (target.path == ext.path || target.path.startsWith(ext.path + File.separator))
        if (!insideBase && !insideExt) {
            throw SecurityException(
                "Android 端只开放 app 私有目录（~ = ${base.path}）。" +
                    "受分区存储限制，读别的位置需要用户手动授权，暂不开放。"
            )
        }
        return target
    }

    private fun fsList(ctx: Context, p: JSONObject): JSONObject {
        val dir = resolve(ctx, p.optString("path", "~"))
        if (!dir.isDirectory) throw IllegalArgumentException("不是目录：${dir.path}")
        val entries = JSONArray()
        val files = dir.listFiles()?.sortedWith(
            compareBy({ !it.isDirectory }, { it.name.lowercase() })
        ) ?: emptyList()
        for (f in files) {
            if (f.name.startsWith(".") && !p.optBoolean("hidden", false)) continue
            entries.put(
                JSONObject()
                    .put("name", f.name)
                    .put("type", if (f.isDirectory) "dir" else "file")
                    .put("size", if (f.isDirectory) JSONObject.NULL else f.length())
                    .put("modified", f.lastModified() / 1000)
            )
        }
        val limit = p.optInt("limit", 200).coerceIn(1, 1000)
        val total = entries.length()
        val sliced = JSONArray()
        for (i in 0 until minOf(total, limit)) sliced.put(entries.get(i))
        return JSONObject().put("path", dir.path).put("total", total)
            .put("entries", sliced).put("truncated", total > limit)
    }

    private fun fsRead(ctx: Context, p: JSONObject): JSONObject {
        val file = resolve(ctx, p.optString("path", ""))
        if (!file.isFile) throw IllegalArgumentException("不是文件：${file.path}")
        val limit = p.optInt("max_bytes", FS_READ_LIMIT).coerceIn(1024, 4_000_000)
        val blob = file.inputStream().use { input ->
            val buf = java.io.ByteArrayOutputStream()
            val chunk = ByteArray(64 * 1024)
            while (buf.size() <= limit) {
                val n = input.read(chunk)
                if (n <= 0) break
                buf.write(chunk, 0, n)
            }
            buf.toByteArray()
        }
        val truncated = blob.size > limit
        val data = blob.copyOf(minOf(blob.size, limit))
        val text = runCatching { data.toString(Charsets.UTF_8) }.getOrNull()
        return if (text != null && !text.contains('\u0000')) {
            JSONObject().put("path", file.path).put("binary", false)
                .put("bytes", data.size).put("truncated", truncated).put("content", text)
        } else {
            JSONObject().put("path", file.path).put("binary", true)
                .put("bytes", data.size).put("truncated", truncated)
                .put("content_b64", Base64.encodeToString(data, Base64.NO_WRAP))
        }
    }

    private fun fsWrite(ctx: Context, p: JSONObject): JSONObject {
        val file = resolve(ctx, p.optString("path", ""))
        if (!p.has("content")) throw IllegalArgumentException("content 不能为空")
        val content = p.optString("content", "")
        val append = p.optString("mode", "overwrite") == "append"
        file.parentFile?.mkdirs()
        val existed = file.exists()
        file.outputStream().use { out ->
            if (append && existed) file.inputStream().use { it.copyTo(out) }
            out.write(content.toByteArray(Charsets.UTF_8))
        }
        return JSONObject().put("path", file.path).put("mode", if (append) "append" else "overwrite")
            .put("existed", existed).put("bytes", content.toByteArray(Charsets.UTF_8).size)
    }
}
