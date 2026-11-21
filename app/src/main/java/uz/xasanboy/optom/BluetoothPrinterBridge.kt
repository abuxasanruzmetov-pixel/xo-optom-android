package uz.xasanboy.optom

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothClass
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothSocket
import android.content.Context
import android.os.Build
import android.util.Base64
import android.webkit.JavascriptInterface
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStream
import java.util.UUID

/**
 * Sahifadagi JavaScript uchun `window.AndroidPrinter` ko'prigi.
 * Telefonga ulangan (paired) Bluetooth Classic (SPP) printerlarga ESC/POS baytlarini yuboradi.
 */
@SuppressLint("MissingPermission")
class BluetoothPrinterBridge(private val activity: MainActivity) {

    private val sppUuid: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
    private var socket: BluetoothSocket? = null
    private var socketAddress: String? = null
    @Volatile private var lastError: String = ""

    private val adapter: BluetoothAdapter?
        get() = (activity.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    private fun hasConnectPermission(): Boolean =
        Build.VERSION.SDK_INT < 31 || activity.checkSelfPermissionCompat(Manifest.permission.BLUETOOTH_CONNECT)

    @JavascriptInterface
    fun requestPermissions() {
        if (Build.VERSION.SDK_INT >= 31 && !hasConnectPermission()) {
            activity.runOnUiThread {
                activity.requestPermissionsCompat(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), MainActivity.REQ_BT)
            }
        }
    }

    @JavascriptInterface
    fun getLastError(): String = lastError

    @JavascriptInterface
    fun isBluetoothOn(): Boolean = adapter?.isEnabled == true

    /** Telefon Bluetooth sozlamalarida ulangan qurilmalar ro'yxati (JSON) */
    @JavascriptInterface
    fun getPairedPrinters(): String {
        val arr = JSONArray()
        if (!hasConnectPermission()) {
            requestPermissions()
            lastError = "Bluetooth ruxsati berilmagan"
            return arr.toString()
        }
        val ad = adapter ?: return arr.toString()
        try {
            for (d in ad.bondedDevices ?: emptySet()) {
                arr.put(JSONObject().apply {
                    put("id", "bt_native_" + d.address)
                    put("name", d.name ?: d.address)
                    put("address", d.address)
                    put("isPrinter", looksLikePrinter(d))
                })
            }
        } catch (e: Exception) {
            lastError = e.message ?: "Ro'yxatni o'qib bo'lmadi"
        }
        return arr.toString()
    }

    private fun looksLikePrinter(d: BluetoothDevice): Boolean {
        val major = try { d.bluetoothClass?.majorDeviceClass } catch (e: Exception) { null }
        if (major == BluetoothClass.Device.Major.IMAGING) return true
        val name = (try { d.name } catch (e: Exception) { null } ?: "").lowercase()
        if (Regex("print|pos|xp-|xp\\d|mht|rpp|mpt|pt-|zj|gp-|thermal|milestone|bluetooth printer|innerprinter|qs|hm-|sprt|goojprt|peripage").containsMatchIn(name)) return true
        return major == null || major == BluetoothClass.Device.Major.UNCATEGORIZED || major == BluetoothClass.Device.Major.MISC
    }

    /** ESC/POS baytlarini (base64) tanlangan printerga yuborish. Muvaffaqiyatli bo'lsa true. */
    @JavascriptInterface
    fun printBase64(b64: String, address: String?, name: String?): Boolean {
        return try {
            val bytes = Base64.decode(b64, Base64.DEFAULT)
            send(bytes, address, name)
            lastError = ""
            true
        } catch (e: Exception) {
            closeSocket()
            lastError = e.message ?: "Printerga yuborib bo'lmadi"
            false
        }
    }

    /** Oddiy matn (zaxira yo'l) */
    @JavascriptInterface
    fun printRawText(text: String, address: String?, name: String?): Boolean {
        return try {
            val init = byteArrayOf(0x1B, 0x40)
            val body = text.toByteArray(Charsets.US_ASCII)
            val tail = byteArrayOf(0x0A, 0x0A, 0x0A, 0x1D, 0x56, 0x42, 0x00)
            send(init + body + tail, address, name)
            lastError = ""
            true
        } catch (e: Exception) {
            closeSocket()
            lastError = e.message ?: "Printerga yuborib bo'lmadi"
            false
        }
    }

    @JavascriptInterface
    fun saveTextFile(fileName: String, content: String): Boolean =
        activity.saveTextToDownloads(fileName, content)

    /** Telefon, xarita (geo:) va veb havolalarni tashqi ilovada ochish */
    @JavascriptInterface
    fun openUrl(url: String): Boolean = activity.openExternal(url)

    @Synchronized
    private fun send(bytes: ByteArray, address: String?, name: String?) {
        if (!hasConnectPermission()) {
            requestPermissions()
            throw IllegalStateException("Bluetooth ruxsati berilmagan. Ruxsat bering va qayta urining.")
        }
        val ad = adapter ?: throw IllegalStateException("Telefonda Bluetooth yo'q")
        if (!ad.isEnabled) throw IllegalStateException("Telefon Bluetooth'i o'chiq. Yoqing va qayta urining.")

        val device = findDevice(ad, address, name)
            ?: throw IllegalStateException("Printer topilmadi. Avval telefon Bluetooth sozlamalarida printerni ulang (PIN: 0000 yoki 1234).")

        var attempt = 0
        while (true) {
            try {
                val out = openStream(ad, device)
                writeChunked(out, bytes)
                return
            } catch (e: Exception) {
                closeSocket()
                attempt++
                if (attempt >= 2) throw IllegalStateException(
                    "\"${device.name ?: device.address}\" printeriga ulanib bo'lmadi. Printer yoqilganini va boshqa telefonga ulanmaganini tekshiring."
                )
                Thread.sleep(400)
            }
        }
    }

    private fun findDevice(ad: BluetoothAdapter, address: String?, name: String?): BluetoothDevice? {
        val bonded = ad.bondedDevices ?: emptySet()
        if (!address.isNullOrBlank()) {
            bonded.firstOrNull { it.address.equals(address, ignoreCase = true) }?.let { return it }
            if (BluetoothAdapter.checkBluetoothAddress(address.uppercase())) return ad.getRemoteDevice(address.uppercase())
        }
        if (!name.isNullOrBlank()) {
            bonded.firstOrNull { (it.name ?: "").equals(name, ignoreCase = true) }?.let { return it }
        }
        return null
    }

    private fun openStream(ad: BluetoothAdapter, device: BluetoothDevice): OutputStream {
        val existing = socket
        if (existing != null && existing.isConnected && socketAddress == device.address) {
            return existing.outputStream
        }
        closeSocket()
        try { ad.cancelDiscovery() } catch (_: Exception) {}

        val candidates = listOf<() -> BluetoothSocket>(
            { device.createRfcommSocketToServiceRecord(sppUuid) },
            { device.createInsecureRfcommSocketToServiceRecord(sppUuid) },
            {
                val m = device.javaClass.getMethod("createRfcommSocket", Int::class.javaPrimitiveType)
                m.invoke(device, 1) as BluetoothSocket
            }
        )
        var lastEx: Exception? = null
        for (make in candidates) {
            try {
                val s = make()
                s.connect()
                socket = s
                socketAddress = device.address
                return s.outputStream
            } catch (e: Exception) {
                lastEx = e
            }
        }
        throw lastEx ?: IllegalStateException("Ulanib bo'lmadi")
    }

    private fun writeChunked(out: OutputStream, bytes: ByteArray) {
        val chunk = 1024
        var i = 0
        while (i < bytes.size) {
            val len = minOf(chunk, bytes.size - i)
            out.write(bytes, i, len)
            out.flush()
            i += len
            Thread.sleep(12) // printer buferi to'lib qolmasligi uchun
        }
    }

    private fun closeSocket() {
        try { socket?.close() } catch (_: Exception) {}
        socket = null
        socketAddress = null
    }

    fun closeAll() = closeSocket()
}
