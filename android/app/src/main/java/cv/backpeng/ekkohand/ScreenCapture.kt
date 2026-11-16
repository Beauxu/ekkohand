package cv.backpeng.ekkohand

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.Looper
import java.io.ByteArrayOutputStream

/**
 * 屏幕截取（MediaProjection）。授权是「一次性的」：手机重启或服务被杀后，
 * 用户要回 App 里再点一次「授权屏幕截取」。这是 Android 的硬性限制，绕不过去。
 */
object ScreenCapture {
    private var projection: MediaProjection? = null
    private var reader: ImageReader? = null
    private var display: VirtualDisplay? = null
    private var width = 0
    private var height = 0
    private var density = 0

    fun isReady(): Boolean = projection != null && reader != null

    fun attach(ctx: Context, resultCode: Int, data: Intent): Boolean {
        release()
        val mpm = ctx.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val mp = mpm.getMediaProjection(resultCode, data) ?: return false
        // Android 14 起必须先注册回调，再做 createVirtualDisplay
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                release()
            }
        }, Handler(Looper.getMainLooper()))

        val dm = ctx.resources.displayMetrics
        width = dm.widthPixels
        height = dm.heightPixels
        density = dm.densityDpi
        val r = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        display = mp.createVirtualDisplay(
            "ekkohand", width, height, density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, null,
        )
        projection = mp
        reader = r
        return true
    }

    fun release() {
        runCatching { display?.release() }
        runCatching { reader?.close() }
        runCatching { projection?.stop() }
        display = null
        reader = null
        projection = null
    }

    /** 取最新一帧，返回 (pngBytes, width, height)。 */
    fun capturePng(maxEdge: Int): Triple<ByteArray, Int, Int>? {
        val r = reader ?: return null
        var image: Image? = null
        try {
            val deadline = System.currentTimeMillis() + 6_000
            while (System.currentTimeMillis() < deadline) {
                image = r.acquireLatestImage()
                if (image != null) break
                Thread.sleep(80)
            }
            val img = image ?: return null
            val plane = img.planes[0]
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val rowPadding = rowStride - pixelStride * width
            val padded = Bitmap.createBitmap(
                width + rowPadding / pixelStride, height, Bitmap.Config.ARGB_8888,
            )
            padded.copyPixelsFromBuffer(plane.buffer)
            var out = Bitmap.createBitmap(padded, 0, 0, width, height)
            if (maxEdge > 0 && maxOf(out.width, out.height) > maxEdge) {
                val scale = maxEdge.toFloat() / maxOf(out.width, out.height)
                val scaled = Bitmap.createScaledBitmap(
                    out, (out.width * scale).toInt(), (out.height * scale).toInt(), true,
                )
                out = scaled
            }
            val bos = ByteArrayOutputStream()
            out.compress(Bitmap.CompressFormat.PNG, 100, bos)
            return Triple(bos.toByteArray(), out.width, out.height)
        } finally {
            runCatching { image?.close() }
        }
    }
}
