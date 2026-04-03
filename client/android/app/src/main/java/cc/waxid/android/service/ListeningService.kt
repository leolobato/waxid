package cc.waxid.android.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Binder
import android.os.IBinder
import android.util.Log
import cc.waxid.android.audio.AudioCaptureManager
import cc.waxid.android.matching.LogEntry
import cc.waxid.android.matching.MatchClient
import cc.waxid.android.matching.MatchState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.StateFlow

class ListeningService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val audioCaptureManager = AudioCaptureManager()
    private lateinit var matchClient: MatchClient

    val state: StateFlow<MatchState> get() = matchClient.state
    val logEntries: StateFlow<List<LogEntry>> get() = matchClient.logEntries
    val queryCount: StateFlow<Int> get() = matchClient.queryCount
    val lastProcessingTimeMs: StateFlow<Double> get() = matchClient.lastProcessingTimeMs

    inner class LocalBinder : Binder() {
        val service: ListeningService get() = this@ListeningService
    }

    private val binder = LocalBinder()

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onCreate() {
        super.onCreate()
        matchClient = MatchClient(audioCaptureManager)
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "onStartCommand")
        startForeground(NOTIFICATION_ID, buildNotification())
        audioCaptureManager.start()
        matchClient.start(scope)
        return START_STICKY
    }

    override fun onDestroy() {
        Log.d(TAG, "onDestroy")
        matchClient.shutdown()
        audioCaptureManager.stop()
        scope.cancel()
        super.onDestroy()
    }

    fun stopListening() {
        Log.d(TAG, "stopListening")
        matchClient.stop()
        audioCaptureManager.stop()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "WaxID Listening",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Shows when WaxID is actively listening"
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(): Notification {
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("WaxID")
            .setContentText("Listening...")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "ListeningService"
        const val CHANNEL_ID = "waxid_listening"
        const val NOTIFICATION_ID = 1
    }
}
