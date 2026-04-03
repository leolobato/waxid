package cc.waxid.android

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.IBinder
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.*
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.preference.PreferenceManager
import cc.waxid.android.service.ControlService
import cc.waxid.android.service.ListeningService
import cc.waxid.android.ui.WebViewScreen
import cc.waxid.android.ui.theme.WaxIDTheme
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private var controlService: ControlService? = null
    private var controlBound = false

    private var listeningService: ListeningService? = null
    private var listeningBound = false

    private val isListening = MutableStateFlow(false)
    private val isConfigured = MutableStateFlow(false)
    private val serverUrl = MutableStateFlow(Config.serverUrl)

    private val autoStartListening = MutableStateFlow(true)
    private val remoteControlEnabled = MutableStateFlow(true)
    private val keepScreenOn = MutableStateFlow(false)

    private var controlListeningJob: Job? = null

    private val controlConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            controlService = (binder as? ControlService.LocalBinder)?.service
            controlBound = true
            // Sync listening state from ControlService (handles remote start/stop)
            controlListeningJob = CoroutineScope(Dispatchers.Main).launch {
                controlService?.isListening?.collect { listening ->
                    isListening.value = listening
                }
            }
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            controlListeningJob?.cancel()
            controlListeningJob = null
            controlService = null
            controlBound = false
        }
    }

    private val listeningConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            listeningService = (binder as? ListeningService.LocalBinder)?.service
            listeningBound = true
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            listeningService = null
            listeningBound = false
            isListening.value = false
        }
    }

    private val requestPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted && autoStartListening.value && isConfigured.value) {
            startListening()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Config.load(this)
        isConfigured.value = Config.isConfigured
        serverUrl.value = Config.serverUrl

        val prefs = PreferenceManager.getDefaultSharedPreferences(this)
        autoStartListening.value = prefs.getBoolean("auto_start_listening", true)
        remoteControlEnabled.value = prefs.getBoolean("remote_control_enabled", true)
        keepScreenOn.value = prefs.getBoolean("keep_screen_on", false)

        updateKeepScreenOn(keepScreenOn.value)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermission.launch(Manifest.permission.RECORD_AUDIO)
        }

        if (remoteControlEnabled.value) {
            startControlService()
        }

        val listeningIntent = Intent(this, ListeningService::class.java)
        bindService(listeningIntent, listeningConnection, 0)

        if (autoStartListening.value && isConfigured.value) {
            startListening()
        }

        setContent {
            WaxIDTheme {
                val listening by isListening.collectAsStateWithLifecycle()
                val serverUrlValue by serverUrl.collectAsStateWithLifecycle()
                val configured by isConfigured.collectAsStateWithLifecycle()
                val autoStart by autoStartListening.collectAsStateWithLifecycle()
                val remoteControl by remoteControlEnabled.collectAsStateWithLifecycle()
                val screenOn by keepScreenOn.collectAsStateWithLifecycle()

                WebViewScreen(
                    serverUrl = serverUrlValue,
                    isConfigured = configured,
                    isListening = listening,
                    autoStartListening = autoStart,
                    remoteControlEnabled = remoteControl,
                    keepScreenOn = screenOn,
                    onConnect = { url ->
                        serverUrl.value = url
                        Config.saveServerUrl(this@MainActivity, url)
                        isConfigured.value = true
                        if (autoStartListening.value) {
                            startListening()
                        }
                    },
                    onStartListening = { startListening() },
                    onStopListening = { stopListening() },
                    onLogout = {
                        stopListening()
                        Config.clearServerUrl(this@MainActivity)
                        serverUrl.value = Config.serverUrl
                        isConfigured.value = false
                    },
                    onAutoStartListeningChange = { value ->
                        autoStartListening.value = value
                        savePref("auto_start_listening", value)
                    },
                    onRemoteControlChange = { value ->
                        remoteControlEnabled.value = value
                        savePref("remote_control_enabled", value)
                        if (value) {
                            startControlService()
                        } else {
                            stopControlService()
                        }
                    },
                    onKeepScreenOnChange = { value ->
                        keepScreenOn.value = value
                        savePref("keep_screen_on", value)
                        updateKeepScreenOn(value)
                    },
                )
            }
        }
    }

    private fun updateKeepScreenOn(enabled: Boolean) {
        if (enabled) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    private fun startControlService() {
        val intent = Intent(this, ControlService::class.java)
        startForegroundService(intent)
        bindService(intent, controlConnection, Context.BIND_AUTO_CREATE)
    }

    private fun stopControlService() {
        if (controlBound) {
            unbindService(controlConnection)
            controlBound = false
        }
        controlService = null
        val intent = Intent(this, ControlService::class.java)
        stopService(intent)
    }

    private fun savePref(key: String, value: Boolean) {
        PreferenceManager.getDefaultSharedPreferences(this)
            .edit().putBoolean(key, value).apply()
    }

    private fun startListening() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermission.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        controlService?.startListening()
        val intent = Intent(this, ListeningService::class.java)
        bindService(intent, listeningConnection, Context.BIND_AUTO_CREATE)
        isListening.value = true
    }

    private fun stopListening() {
        controlService?.stopListening()
        if (listeningBound) {
            unbindService(listeningConnection)
            listeningBound = false
            listeningService = null
        }
        isListening.value = false
    }

    override fun onDestroy() {
        if (listeningBound) unbindService(listeningConnection)
        if (controlBound) unbindService(controlConnection)
        super.onDestroy()
    }
}
