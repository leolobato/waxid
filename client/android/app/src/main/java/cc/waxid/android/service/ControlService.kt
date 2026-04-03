package cc.waxid.android.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Binder
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import cc.waxid.android.Config
import cc.waxid.android.control.ControlServer
import cc.waxid.android.matching.MatchState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

class ControlService : Service() {
    private var controlServer: ControlServer? = null
    private var listeningService: ListeningService? = null
    private var listeningBound = false
    private val mainHandler = Handler(Looper.getMainLooper())

    val listeningServiceRef: ListeningService? get() = listeningService

    private val _isListening = MutableStateFlow(false)
    val isListening: StateFlow<Boolean> = _isListening

    inner class LocalBinder : Binder() {
        val service: ControlService get() = this@ControlService
    }

    private val binder = LocalBinder()

    override fun onBind(intent: Intent?): IBinder = binder

    private val listeningConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            listeningService = (binder as? ListeningService.LocalBinder)?.service
            listeningBound = true
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            listeningService = null
            listeningBound = false
            _isListening.value = false
        }
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        controlServer = ControlServer(
            onStart = { mainHandler.post { startListening() } },
            onStop = { mainHandler.post { stopListening() } },
            getState = { listeningService?.state?.value ?: MatchState.Idle }
        )
    }

    private var serverStarted = false

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification())
        if (!serverStarted) {
            controlServer?.start()
            serverStarted = true
        }
        return START_STICKY
    }

    override fun onDestroy() {
        controlServer?.stop()
        if (listeningBound) {
            unbindService(listeningConnection)
        }
        super.onDestroy()
    }

    fun startListening() {
        val intent = Intent(this, ListeningService::class.java)
        startForegroundService(intent)
        bindService(intent, listeningConnection, Context.BIND_AUTO_CREATE)
        _isListening.value = true
    }

    fun stopListening() {
        listeningService?.stopListening()
        if (listeningBound) {
            unbindService(listeningConnection)
            listeningBound = false
            listeningService = null
        }
        _isListening.value = false
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "WaxID Control",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "WaxID control server is running"
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("WaxID")
            .setContentText("Ready — control server on port ${Config.controlPort}")
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()
    }

    companion object {
        const val CHANNEL_ID = "waxid_control"
        const val NOTIFICATION_ID = 2
    }
}
