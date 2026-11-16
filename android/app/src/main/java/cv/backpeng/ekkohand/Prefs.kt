package cv.backpeng.ekkohand

import android.content.Context

/** 配对信息。token 只存本机 SharedPreferences，服务器只存哈希。 */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("ekkohand", Context.MODE_PRIVATE)

    var hub: String
        get() = sp.getString("hub", DEFAULT_HUB) ?: DEFAULT_HUB
        set(value) = sp.edit().putString("hub", value.trimEnd('/')).apply()

    var deviceId: String
        get() = sp.getString("device_id", "") ?: ""
        set(value) = sp.edit().putString("device_id", value).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(value) = sp.edit().putString("token", value).apply()

    var deviceName: String
        get() = sp.getString("name", "") ?: ""
        set(value) = sp.edit().putString("name", value).apply()

    val paired: Boolean get() = token.isNotEmpty() && deviceId.isNotEmpty()

    fun save(hubUrl: String, id: String, tok: String, name: String) {
        sp.edit().putString("hub", hubUrl.trimEnd('/')).putString("device_id", id)
            .putString("token", tok).putString("name", name).apply()
    }

    fun clear() = sp.edit().clear().apply()

    companion object {
        const val DEFAULT_HUB = "https://47.88.53.79/hand"
        const val VERSION = "1.0.0"
    }
}
