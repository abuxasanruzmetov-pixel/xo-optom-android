package uz.xasanboy.optom

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import java.io.File

class MainActivity : Activity() {

    private lateinit var webView: WebView
    private lateinit var printer: BluetoothPrinterBridge
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingWebPermission: PermissionRequest? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        setContentView(webView)

        printer = BluetoothPrinterBridge(this)

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true          // localStorage (baza) uchun
            databaseEnabled = true
            allowFileAccess = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
        }
        webView.addJavascriptInterface(printer, "AndroidPrinter")
        webView.webViewClient = WebViewClient()
        webView.webChromeClient = object : WebChromeClient() {
            // Kamera (shtrix-kod skaneri)
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    if (checkSelfPermissionCompat(Manifest.permission.CAMERA)) {
                        request.grant(request.resources)
                    } else {
                        pendingWebPermission = request
                        requestPermissionsCompat(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
                    }
                }
            }

            // JSON import uchun fayl tanlash
            override fun onShowFileChooser(
                view: WebView?,
                callback: ValueCallback<Array<Uri>>?,
                params: FileChooserParams?
            ): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                }
                return try {
                    startActivityForResult(Intent.createChooser(intent, "Faylni tanlang"), REQ_FILE)
                    true
                } catch (e: Exception) {
                    fileCallback = null
                    false
                }
            }
        }

        webView.loadUrl("file:///android_asset/index.html")
        printer.requestPermissions()
    }

    fun checkSelfPermissionCompat(p: String): Boolean =
        Build.VERSION.SDK_INT < 23 || checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    fun requestPermissionsCompat(perms: Array<String>, code: Int) {
        if (Build.VERSION.SDK_INT >= 23) requestPermissions(perms, code)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        when (requestCode) {
            REQ_CAMERA -> {
                val req = pendingWebPermission
                pendingWebPermission = null
                if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                    req?.grant(req.resources)
                } else {
                    req?.deny()
                }
            }
            REQ_BT -> {
                // Ruxsat berilgach printerlar ro'yxatini yangilash
                webView.evaluateJavascript(
                    "window.refreshPairedBluetoothDevices && refreshPairedBluetoothDevices(false);", null
                )
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_FILE) {
            val uri = if (resultCode == RESULT_OK) data?.data else null
            fileCallback?.onReceiveValue(if (uri != null) arrayOf(uri) else null)
            fileCallback = null
        }
    }

    /** JSON zaxira faylini "Yuklanmalar" (Downloads) papkasiga saqlash */
    fun saveTextToDownloads(fileName: String, content: String): Boolean {
        return try {
            if (Build.VERSION.SDK_INT >= 29) {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, fileName)
                    put(MediaStore.Downloads.MIME_TYPE, "application/json")
                    put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                }
                val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: return false
                contentResolver.openOutputStream(uri)?.use { it.write(content.toByteArray(Charsets.UTF_8)) }
            } else {
                val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: filesDir
                File(dir, fileName).writeText(content, Charsets.UTF_8)
            }
            runOnUiThread { Toast.makeText(this, "Saqlandi: Yuklanmalar/$fileName", Toast.LENGTH_LONG).show() }
            true
        } catch (e: Exception) {
            false
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        // Ochiq oyna bo'lsa — uni yopamiz, aks holda ilovadan chiqamiz
        webView.evaluateJavascript(
            "(function(){var b=document.getElementById('app-modal-backdrop');" +
                "if(b&&!b.classList.contains('hidden')){closeAppModal();return 'closed';}return 'none';})()"
        ) { res ->
            if (res?.contains("closed") != true) finish()
        }
    }

    override fun onDestroy() {
        printer.closeAll()
        super.onDestroy()
    }

    companion object {
        const val REQ_CAMERA = 11
        const val REQ_BT = 12
        const val REQ_FILE = 13
    }
}
