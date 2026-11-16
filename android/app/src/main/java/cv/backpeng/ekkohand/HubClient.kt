package cv.backpeng.ekkohand

import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** 极简 HTTP：只用 HttpURLConnection + org.json，不引 OkHttp。 */
object HubClient {

    data class Response(val status: Int, val body: JSONObject)

    fun post(hub: String, path: String, payload: JSONObject, timeoutMs: Int): Response {
        val url = URL(hub.trimEnd('/') + path)
        val conn = (url.openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 12_000
            readTimeout = timeoutMs
            doOutput = true
            setRequestProperty("Content-Type", "application/json; charset=utf-8")
            setRequestProperty("Accept", "application/json")
            setRequestProperty("User-Agent", "EkkoHand/${Prefs.VERSION} (android)")
        }
        try {
            conn.outputStream.use { it.write(payload.toString().toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            val body = if (text.isBlank()) JSONObject()
            else runCatching { JSONObject(text) }
                .getOrElse { JSONObject().put("error", text.take(300)) }
            return Response(code, body)
        } finally {
            conn.disconnect()
        }
    }

    fun pair(hub: String, code: String, name: String, fingerprint: String,
             caps: List<String>): Response =
        post(hub, "/pair", JSONObject()
            .put("code", code)
            .put("name", name)
            .put("fingerprint", fingerprint)
            .put("caps", JSONArray(caps)), 20_000)
}
