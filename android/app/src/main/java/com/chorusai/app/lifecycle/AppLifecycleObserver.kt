package com.chorusai.app.lifecycle

import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import com.chorusai.app.network.WebSocketManager
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class AppLifecycleObserver @Inject constructor(
    private val webSocketManager: WebSocketManager
) : DefaultLifecycleObserver {

    override fun onStart(owner: LifecycleOwner) {
        webSocketManager.onAppForeground()
    }

    override fun onStop(owner: LifecycleOwner) {
        webSocketManager.onAppBackground()
    }
}
