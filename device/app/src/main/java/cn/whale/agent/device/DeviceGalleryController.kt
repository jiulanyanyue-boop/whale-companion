package cn.whale.agent.device

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.util.Base64
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream

class GalleryPermissionException : SecurityException("gallery permission required")

fun galleryDecodeSample(width: Int, height: Int, maxEdge: Int): Int {
    require(width > 0 && height > 0 && maxEdge > 0)
    var sample = 1
    while (maxOf(width, height) / (sample * 2) >= maxEdge) sample *= 2
    return sample
}

/** Bounded, read-only access to recent images; IDs are MediaStore row IDs, never paths. */
class DeviceGalleryController(private val context: Context) {
    fun list(limit: Int, offset: Int): List<Map<String, String>> {
        requireImagePermission()
        val collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI
        val columns = arrayOf("_id", "date_added", "mime_type", "width", "height", "_size")
        val result = mutableListOf<Map<String, String>>()
        val query = Bundle().apply {
            putString(android.content.ContentResolver.QUERY_ARG_SQL_SORT_ORDER, "date_added DESC")
            putInt(android.content.ContentResolver.QUERY_ARG_LIMIT, limit)
            putInt(android.content.ContentResolver.QUERY_ARG_OFFSET, offset)
        }
        context.contentResolver.query(collection, columns, query, null)?.use { cursor ->
            val id = cursor.getColumnIndexOrThrow("_id")
            val date = cursor.getColumnIndexOrThrow("date_added")
            val mime = cursor.getColumnIndexOrThrow("mime_type")
            val width = cursor.getColumnIndexOrThrow("width")
            val height = cursor.getColumnIndexOrThrow("height")
            val size = cursor.getColumnIndexOrThrow("_size")
            while (cursor.moveToNext() && result.size < limit) {
                result += mapOf("media_id" to cursor.getLong(id).toString(), "date_added" to cursor.getLong(date).toString(),
                    "mime" to cursor.getString(mime).orEmpty(), "width" to cursor.getInt(width).toString(),
                    "height" to cursor.getInt(height).toString(), "size" to cursor.getLong(size).toString())
            }
        }
        return result
    }

    fun preview(rawId: String): Map<String, String> {
        requireImagePermission()
        val id = rawId.toLongOrNull()?.takeIf { it > 0 } ?: throw IllegalArgumentException("invalid media_id")
        val uri = android.content.ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id)
        val resolver = context.contentResolver
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
        require(bounds.outWidth > 0 && bounds.outHeight > 0) { "image is unavailable" }
        // Keep enough decoded pixels for readable screenshots before scaling.
        val sample = galleryDecodeSample(bounds.outWidth, bounds.outHeight, MAX_EDGE)
        var bitmap = resolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply { inSampleSize = sample })
        } ?: throw IllegalArgumentException("image is unavailable")
        val maxDimension = maxOf(bitmap.width, bitmap.height)
        if (maxDimension > MAX_EDGE) {
            val scale = MAX_EDGE.toFloat() / maxDimension
            val resized = Bitmap.createScaledBitmap(bitmap, (bitmap.width * scale).toInt().coerceAtLeast(1), (bitmap.height * scale).toInt().coerceAtLeast(1), true)
            if (resized !== bitmap) bitmap.recycle()
            bitmap = resized
        }
        try {
            var quality = 92
            var bytes = ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
            while (bytes.size > MAX_BYTES && quality > 20) {
                quality = (quality - 10).coerceAtLeast(20)
                bytes = ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
            }
            if (bytes.size > MAX_BYTES) throw IllegalArgumentException("preview exceeds size limit")
            return mapOf("media_id" to id.toString(), "mime" to "image/jpeg", "width" to bitmap.width.toString(),
                "height" to bitmap.height.toString(), "image_base64" to Base64.encodeToString(bytes, Base64.NO_WRAP))
        } finally { bitmap.recycle() }
    }

    private fun requireImagePermission() {
        val permission = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_IMAGES else Manifest.permission.READ_EXTERNAL_STORAGE
        if (ContextCompat.checkSelfPermission(context, permission) != PackageManager.PERMISSION_GRANTED) throw GalleryPermissionException()
    }

    companion object { private const val MAX_EDGE = 2560; private const val MAX_BYTES = 600 * 1024 }
}
