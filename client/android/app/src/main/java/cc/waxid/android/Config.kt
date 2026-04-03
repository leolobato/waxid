package cc.waxid.android

import android.content.Context
import androidx.preference.PreferenceManager
import java.util.Properties

object Config {
    var serverUrl: String = "http://10.0.1.9:8457"
        private set
    var controlPort: Int = 8458
        private set
    var isConfigured: Boolean = false
        private set

    fun load(context: Context) {
        // Load defaults from assets
        try {
            context.assets.open("config.properties").use { stream ->
                val props = Properties()
                props.load(stream)
                serverUrl = props.getProperty("server_url", serverUrl)
                controlPort = props.getProperty("control_port", "$controlPort").toInt()
            }
        } catch (_: Exception) {
            // Use defaults
        }
        // SharedPreferences override (runtime changes)
        val prefs = PreferenceManager.getDefaultSharedPreferences(context)
        prefs.getString("server_url", null)?.let { serverUrl = it }
        isConfigured = prefs.getBoolean("configured", false)
    }

    fun saveServerUrl(context: Context, url: String) {
        serverUrl = url
        isConfigured = true
        PreferenceManager.getDefaultSharedPreferences(context)
            .edit()
            .putString("server_url", url)
            .putBoolean("configured", true)
            .apply()
    }

    fun clearServerUrl(context: Context) {
        isConfigured = false
        PreferenceManager.getDefaultSharedPreferences(context)
            .edit()
            .putBoolean("configured", false)
            .apply()
    }
}
