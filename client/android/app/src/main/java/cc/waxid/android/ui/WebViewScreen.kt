package cc.waxid.android.ui

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView

@OptIn(ExperimentalMaterial3Api::class)
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun WebViewScreen(
    serverUrl: String,
    isConfigured: Boolean,
    isListening: Boolean,
    autoStartListening: Boolean,
    remoteControlEnabled: Boolean,
    keepScreenOn: Boolean,
    onConnect: (String) -> Unit,
    onStartListening: () -> Unit,
    onStopListening: () -> Unit,
    onLogout: () -> Unit,
    onAutoStartListeningChange: (Boolean) -> Unit,
    onRemoteControlChange: (Boolean) -> Unit,
    onKeepScreenOnChange: (Boolean) -> Unit,
) {
    if (!isConfigured) {
        SetupScreen(onConnect = onConnect)
        return
    }

    val currentIsListening = rememberUpdatedState(isListening)
    val currentOnStart = rememberUpdatedState(onStartListening)
    val currentOnStop = rememberUpdatedState(onStopListening)
    val currentOnLogout = rememberUpdatedState(onLogout)

    var showSettingsSheet by remember { mutableStateOf(false) }
    var webView by remember { mutableStateOf<WebView?>(null) }
    var loadError by remember { mutableStateOf(false) }
    var currentUrl by remember { mutableStateOf(serverUrl) }

    LaunchedEffect(serverUrl) {
        if (serverUrl != currentUrl) {
            currentUrl = serverUrl
            loadError = false
            webView?.loadUrl("$serverUrl/")
        }
    }

    // Push listening state changes to the web UI immediately
    LaunchedEffect(Unit) {
        snapshotFlow { currentIsListening.value }
            .collect { listening ->
                webView?.post {
                    webView?.evaluateJavascript(
                        "if(window.setClientListening){window.setClientListening($listening)}",
                        null
                    )
                }
            }
    }

    Box(modifier = Modifier.fillMaxSize()) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { context ->
                WebView(context).apply {
                    layoutParams = ViewGroup.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT,
                    )
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    settings.allowFileAccess = false
                    settings.allowContentAccess = false
                    settings.builtInZoomControls = false
                    settings.displayZoomControls = false
                    settings.userAgentString =
                        "${settings.userAgentString} WaxID-Android/1.0"
                    setLayerType(android.view.View.LAYER_TYPE_HARDWARE, null)

                    addJavascriptInterface(
                        WaxIDBridge(
                            onStart = {
                                Handler(Looper.getMainLooper()).post {
                                    currentOnStart.value()
                                }
                            },
                            onStop = {
                                Handler(Looper.getMainLooper()).post {
                                    currentOnStop.value()
                                }
                            },
                            onOpenSettings = {
                                Handler(Looper.getMainLooper()).post {
                                    showSettingsSheet = true
                                }
                            },
                            isListeningProvider = { currentIsListening.value },
                        ),
                        "WaxID",
                    )

                    webViewClient = object : WebViewClient() {
                        override fun shouldOverrideUrlLoading(
                            view: WebView?, request: WebResourceRequest?
                        ): Boolean {
                            val url = request?.url?.toString() ?: return false
                            return !url.startsWith(serverUrl)
                        }

                        override fun onPageStarted(
                            view: WebView?, url: String?, favicon: Bitmap?
                        ) {
                            loadError = false
                        }

                        override fun onReceivedError(
                            view: WebView?, request: WebResourceRequest?,
                            error: WebResourceError?
                        ) {
                            if (request?.isForMainFrame == true) {
                                loadError = true
                            }
                        }
                    }
                    webChromeClient = WebChromeClient()

                    webView = this
                    loadUrl("$serverUrl/")
                }
            },
        )

        if (loadError) {
            Surface(
                modifier = Modifier.fillMaxSize(),
                color = Color(0xFF0A0A0A),
            ) {
                Column(
                    modifier = Modifier.fillMaxSize(),
                    verticalArrangement = Arrangement.Center,
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Text(
                        "Could not reach server",
                        color = Color.White.copy(alpha = 0.6f),
                        fontSize = 18.sp,
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        serverUrl,
                        color = Color.White.copy(alpha = 0.3f),
                        fontSize = 14.sp,
                    )
                    Spacer(Modifier.height(24.dp))
                    Button(onClick = {
                        loadError = false
                        webView?.loadUrl("$serverUrl/")
                    }) {
                        Text("Retry")
                    }
                    Spacer(Modifier.height(8.dp))
                    TextButton(onClick = { onLogout() }) {
                        Text(
                            "Change Server",
                            color = Color.White.copy(alpha = 0.4f),
                        )
                    }
                }
            }

            LaunchedEffect(loadError) {
                if (loadError) {
                    kotlinx.coroutines.delay(10_000)
                    loadError = false
                    webView?.loadUrl("$serverUrl/")
                }
            }
        }

        if (showSettingsSheet) {
            ModalBottomSheet(
                onDismissRequest = { showSettingsSheet = false },
                containerColor = Color(0xFF161616),
                contentColor = Color(0xFFE0E0E0),
            ) {
                SettingsPanel(
                    autoStartListening = autoStartListening,
                    remoteControlEnabled = remoteControlEnabled,
                    keepScreenOn = keepScreenOn,
                    onAutoStartListeningChange = onAutoStartListeningChange,
                    onRemoteControlChange = onRemoteControlChange,
                    onKeepScreenOnChange = onKeepScreenOnChange,
                    onLogout = {
                        showSettingsSheet = false
                        onLogout()
                    },
                )
            }
        }
    }
}

@Composable
private fun SettingsPanel(
    autoStartListening: Boolean,
    remoteControlEnabled: Boolean,
    keepScreenOn: Boolean,
    onAutoStartListeningChange: (Boolean) -> Unit,
    onRemoteControlChange: (Boolean) -> Unit,
    onKeepScreenOnChange: (Boolean) -> Unit,
    onLogout: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 24.dp)
            .padding(bottom = 32.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Text(
            "App Settings",
            fontWeight = FontWeight.Medium,
            fontSize = 16.sp,
            modifier = Modifier.padding(bottom = 12.dp),
        )

        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Auto-start listening", fontSize = 15.sp)
                Text(
                    "Begin capturing audio on app launch",
                    fontSize = 12.sp,
                    color = Color(0xFF888888),
                )
            }
            Switch(checked = autoStartListening, onCheckedChange = onAutoStartListeningChange)
        }

        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Remote control", fontSize = 15.sp)
                Text(
                    "Allow start/stop via network",
                    fontSize = 12.sp,
                    color = Color(0xFF888888),
                )
            }
            Switch(checked = remoteControlEnabled, onCheckedChange = onRemoteControlChange)
        }

        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text("Keep screen on", fontSize = 15.sp)
                Text(
                    "Prevent the screen from sleeping",
                    fontSize = 12.sp,
                    color = Color(0xFF888888),
                )
            }
            Switch(checked = keepScreenOn, onCheckedChange = onKeepScreenOnChange)
        }

        HorizontalDivider(
            color = Color(0xFF2A2A2A),
            modifier = Modifier.padding(vertical = 12.dp),
        )

        TextButton(
            onClick = onLogout,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(
                "Logout",
                color = Color(0xFFE55555),
                fontSize = 15.sp,
            )
        }
    }
}

@Composable
private fun SetupScreen(onConnect: (String) -> Unit) {
    var serverUrl by remember { mutableStateOf("") }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color(0xFF0A0A0A)),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier
                .widthIn(max = 360.dp)
                .padding(horizontal = 32.dp),
        ) {
            VinylIcon()

            Spacer(Modifier.height(24.dp))

            Text(
                "WaxID",
                color = Color.White,
                fontSize = 28.sp,
                fontWeight = FontWeight.SemiBold,
                letterSpacing = (-0.5).sp,
            )

            Spacer(Modifier.height(8.dp))

            Text(
                "Enter your server address to get started",
                color = Color.White.copy(alpha = 0.4f),
                fontSize = 14.sp,
            )

            Spacer(Modifier.height(32.dp))

            OutlinedTextField(
                value = serverUrl,
                onValueChange = { serverUrl = it },
                label = { Text("Server Address") },
                placeholder = { Text("http://192.168.1.100:8457") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(
                    onDone = {
                        val url = normalizeUrl(serverUrl)
                        if (url.isNotBlank()) onConnect(url)
                    },
                ),
            )

            Spacer(Modifier.height(20.dp))

            Button(
                onClick = {
                    val url = normalizeUrl(serverUrl)
                    if (url.isNotBlank()) onConnect(url)
                },
                enabled = serverUrl.isNotBlank(),
                modifier = Modifier
                    .fillMaxWidth()
                    .height(48.dp),
                shape = RoundedCornerShape(8.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Color(0xFF4A9EFF),
                ),
            ) {
                Text("Connect", fontSize = 16.sp)
            }
        }
    }
}

@Composable
private fun VinylIcon(modifier: Modifier = Modifier) {
    val infiniteTransition = rememberInfiniteTransition(label = "vinyl-spin")
    val rotation by infiniteTransition.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 8000, easing = LinearEasing),
        ),
        label = "rotation",
    )

    Canvas(
        modifier = modifier
            .size(80.dp)
            .rotate(rotation),
    ) {
        val center = Offset(size.width / 2, size.height / 2)
        val strokeWidth = 1.5.dp.toPx()
        val outerRadius = size.minDimension / 2 - strokeWidth
        val innerRadius = outerRadius * 0.3f

        drawCircle(
            color = Color.White.copy(alpha = 0.3f),
            radius = outerRadius,
            center = center,
            style = Stroke(width = strokeWidth),
        )
        drawCircle(
            color = Color.White.copy(alpha = 0.3f),
            radius = innerRadius,
            center = center,
            style = Stroke(width = strokeWidth),
        )
    }
}

private fun normalizeUrl(input: String): String {
    val trimmed = input.trim()
    if (trimmed.isBlank()) return ""
    val withScheme = if (!trimmed.startsWith("http://") && !trimmed.startsWith("https://")) {
        "http://$trimmed"
    } else {
        trimmed
    }
    return withScheme.trimEnd('/')
}

private class WaxIDBridge(
    private val onStart: () -> Unit,
    private val onStop: () -> Unit,
    private val onOpenSettings: () -> Unit,
    private val isListeningProvider: () -> Boolean,
) {
    @JavascriptInterface
    fun startListening() = onStart()

    @JavascriptInterface
    fun stopListening() = onStop()

    @JavascriptInterface
    fun openSettings() = onOpenSettings()

    @JavascriptInterface
    fun isListening(): Boolean = isListeningProvider()
}
