package com.chorusai.app.network

import android.util.Log
import com.chorusai.app.model.ChatMessage
import com.chorusai.app.model.PipelineState
import com.chorusai.app.model.WsEvent
import com.google.gson.Gson
import com.google.gson.JsonObject
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.logging.HttpLoggingInterceptor
import java.net.URLEncoder
import javax.inject.Inject
import javax.inject.Named
import javax.inject.Singleton

@Singleton
class WebSocketManager @Inject constructor(
    okHttpClient: OkHttpClient,
    private val gson: Gson,
    @Named("baseUrl") baseUrl: String
) {
    companion object {
        private const val TAG = "WebSocketManager"
        private const val PING_INTERVAL_MS = 30_000L
        private const val MAX_RECONNECT_ATTEMPTS = 5
    }

    // Downgrade BODY-level logging to HEADERS — BODY interferes with WebSocket frames
    private val wsHttpClient = okHttpClient.newBuilder()
        .apply {
            val bodyLoggers = interceptors().filterIsInstance<HttpLoggingInterceptor>()
            bodyLoggers.forEach { interceptors().remove(it) }
            if (bodyLoggers.isNotEmpty()) {
                addInterceptor(HttpLoggingInterceptor().apply {
                    level = HttpLoggingInterceptor.Level.HEADERS
                })
            }
        }
        .build()

    private val wsBaseUrl: String = baseUrl.trimEnd('/').let { url ->
        when {
            url.startsWith("https://") -> "wss://" + url.removePrefix("https://")
            url.startsWith("http://") -> "ws://" + url.removePrefix("http://")
            else -> url
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val lock = Any()

    // User-level WebSocket
    private var userWs: WebSocket? = null
    private var userPingJob: Job? = null
    private var userReconnectJob: Job? = null
    private var userReconnectAttempts = 0
    private var currentUsername: String? = null
    private var userDisconnectRequested = false

    private val _userEvents = MutableSharedFlow<WsEvent>(extraBufferCapacity = 64)
    val userEvents: SharedFlow<WsEvent> = _userEvents

    // Chat-level WebSocket
    private var chatWs: WebSocket? = null
    private var chatPingJob: Job? = null
    private var chatReconnectJob: Job? = null
    private var chatReconnectAttempts = 0
    private var chatUsername: String? = null
    private var currentChatName: String? = null
    private var currentProject: String? = null
    private var chatDisconnectRequested = false

    private val _chatEvents = MutableSharedFlow<WsEvent>(extraBufferCapacity = 64)
    val chatEvents: SharedFlow<WsEvent> = _chatEvents

    private var isInForeground = true

    // --- User-level WS ---

    fun connectUser(username: String) = synchronized(lock) {
        if (username == currentUsername && userWs != null) return
        disconnectUserLocked()
        currentUsername = username
        userDisconnectRequested = false
        userReconnectAttempts = 0
        openUserWsLocked()
    }

    fun disconnectUser() = synchronized(lock) {
        disconnectUserLocked()
    }

    private fun disconnectUserLocked() {
        userDisconnectRequested = true
        userReconnectJob?.cancel()
        userPingJob?.cancel()
        userWs?.close(1000, "disconnect")
        userWs = null
        currentUsername = null
    }

    private fun openUserWsLocked() {
        val username = currentUsername ?: return
        val url = "$wsBaseUrl/api/ws/user/${encode(username)}"
        Log.d(TAG, "Connecting user WS: $url")

        val request = Request.Builder().url(url).build()
        userWs = wsHttpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = synchronized(lock) {
                Log.d(TAG, "User WS opened")
                userReconnectAttempts = 0
                userPingJob?.cancel()
                userPingJob = startPing { synchronized(lock) { userWs } }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                parseUserEvent(text)?.let { _userEvents.tryEmit(it) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = synchronized(lock) {
                if (webSocket != userWs) return
                Log.w(TAG, "User WS failure: ${t.message}")
                userPingJob?.cancel()
                userWs = null
                if (!userDisconnectRequested) scheduleUserReconnectLocked()
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = synchronized(lock) {
                if (webSocket != userWs) return
                Log.d(TAG, "User WS closed: $code $reason")
                userPingJob?.cancel()
                userWs = null
                if (!userDisconnectRequested && isInForeground && currentUsername != null) {
                    scheduleUserReconnectLocked()
                }
            }
        })
    }

    private fun scheduleUserReconnectLocked() {
        if (userReconnectAttempts >= MAX_RECONNECT_ATTEMPTS || !isInForeground) return
        userReconnectJob?.cancel()
        userReconnectJob = scope.launch {
            val attempt = synchronized(lock) { userReconnectAttempts }
            val delayMs = (1000L shl attempt).coerceAtMost(16_000L)
            Log.d(TAG, "User WS reconnect in ${delayMs}ms (attempt ${attempt + 1})")
            delay(delayMs)
            synchronized(lock) {
                if (userDisconnectRequested) return@launch
                userReconnectAttempts++
                openUserWsLocked()
            }
        }
    }

    // --- Chat-level WS ---

    fun connectChat(username: String, chatName: String, project: String?) = synchronized(lock) {
        if (chatName == currentChatName && project == currentProject && chatWs != null) return
        disconnectChatLocked()
        chatUsername = username
        currentChatName = chatName
        currentProject = project
        chatDisconnectRequested = false
        chatReconnectAttempts = 0
        openChatWsLocked()
    }

    fun disconnectChat() = synchronized(lock) {
        disconnectChatLocked()
    }

    private fun disconnectChatLocked() {
        chatDisconnectRequested = true
        chatReconnectJob?.cancel()
        chatPingJob?.cancel()
        chatWs?.close(1000, "disconnect")
        chatWs = null
        chatUsername = null
        currentChatName = null
        currentProject = null
    }

    private fun openChatWsLocked() {
        val username = chatUsername ?: return
        val chatName = currentChatName ?: return
        val url = buildString {
            append("$wsBaseUrl/api/ws/chat/${encode(username)}/${encode(chatName)}")
            currentProject?.let { append("?project=${encode(it)}") }
        }
        Log.d(TAG, "Connecting chat WS: $url")

        val request = Request.Builder().url(url).build()
        chatWs = wsHttpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = synchronized(lock) {
                Log.d(TAG, "Chat WS opened")
                chatReconnectAttempts = 0
                chatPingJob?.cancel()
                chatPingJob = startPing { synchronized(lock) { chatWs } }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                parseChatEvent(text)?.let { _chatEvents.tryEmit(it) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = synchronized(lock) {
                if (webSocket != chatWs) return
                Log.w(TAG, "Chat WS failure: ${t.message}")
                chatPingJob?.cancel()
                chatWs = null
                if (!chatDisconnectRequested) scheduleChatReconnectLocked()
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = synchronized(lock) {
                if (webSocket != chatWs) return
                Log.d(TAG, "Chat WS closed: $code $reason")
                chatPingJob?.cancel()
                chatWs = null
                if (!chatDisconnectRequested && isInForeground && currentChatName != null) {
                    scheduleChatReconnectLocked()
                }
            }
        })
    }

    private fun scheduleChatReconnectLocked() {
        if (chatReconnectAttempts >= MAX_RECONNECT_ATTEMPTS || !isInForeground) return
        chatReconnectJob?.cancel()
        chatReconnectJob = scope.launch {
            val attempt = synchronized(lock) { chatReconnectAttempts }
            val delayMs = (1000L shl attempt).coerceAtMost(16_000L)
            Log.d(TAG, "Chat WS reconnect in ${delayMs}ms (attempt ${attempt + 1})")
            delay(delayMs)
            synchronized(lock) {
                if (chatDisconnectRequested) return@launch
                chatReconnectAttempts++
                openChatWsLocked()
            }
        }
    }

    // --- Lifecycle ---

    fun onAppForeground() = synchronized(lock) {
        isInForeground = true
        userDisconnectRequested = false
        chatDisconnectRequested = false
        if (currentUsername != null && userWs == null) {
            userReconnectAttempts = 0
            openUserWsLocked()
        }
        if (currentChatName != null && chatWs == null) {
            chatReconnectAttempts = 0
            openChatWsLocked()
        }
    }

    fun onAppBackground() = synchronized(lock) {
        isInForeground = false
        userDisconnectRequested = true
        chatDisconnectRequested = true
        userReconnectJob?.cancel()
        chatReconnectJob?.cancel()
        userPingJob?.cancel()
        chatPingJob?.cancel()
        userWs?.close(1000, "background")
        userWs = null
        chatWs?.close(1000, "background")
        chatWs = null
    }

    // --- Ping ---

    private fun startPing(wsProvider: () -> WebSocket?): Job = scope.launch {
        while (true) {
            delay(PING_INTERVAL_MS)
            wsProvider()?.send("{\"type\":\"ping\"}") ?: break
        }
    }

    // --- Helpers ---

    private fun encode(s: String): String = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

    // --- Parsing ---

    private fun parseUserEvent(text: String): WsEvent? {
        return try {
            val json = gson.fromJson(text, JsonObject::class.java)
            val type = json.get("type")?.asString ?: return null
            val data = json.getAsJsonObject("data") ?: JsonObject()
            when (type) {
                "chat_created" -> WsEvent.ChatCreated(
                    chatName = data.get("chat_name").asString,
                    project = data.get("project")?.takeIf { !it.isJsonNull }?.asString
                )
                "chat_deleted" -> WsEvent.ChatDeleted(
                    chatName = data.get("chat_name").asString,
                    project = data.get("project")?.takeIf { !it.isJsonNull }?.asString
                )
                else -> null
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to parse user WS event: $text", e)
            null
        }
    }

    private fun parseChatEvent(text: String): WsEvent? {
        return try {
            val json = gson.fromJson(text, JsonObject::class.java)
            val type = json.get("type")?.asString ?: return null
            val data = json.getAsJsonObject("data") ?: JsonObject()
            when (type) {
                "client_joined" -> WsEvent.ClientJoined(
                    connectionCount = data.get("connection_count").asInt
                )
                "client_left" -> WsEvent.ClientLeft(
                    connectionCount = data.get("connection_count").asInt
                )
                "user_message_added" -> WsEvent.UserMessageAdded(
                    message = gson.fromJson(data.get("message"), ChatMessage::class.java),
                    currentLeafId = data.get("current_leaf_id")?.takeIf { !it.isJsonNull }?.asString
                )
                "stream_content" -> WsEvent.StreamContent(
                    delta = data.get("delta").asString
                )
                "stream_thinking" -> WsEvent.StreamThinking(
                    delta = data.get("delta").asString
                )
                "stream_done" -> {
                    val assistantMsg = gson.fromJson(data.get("assistant_message"), ChatMessage::class.java)
                    val ps = data.get("pipeline_state")?.takeIf { !it.isJsonNull }?.let {
                        gson.fromJson(it, PipelineState::class.java)
                    }
                    WsEvent.StreamDone(
                        assistantMessage = assistantMsg,
                        currentLeafId = data.get("current_leaf_id")?.takeIf { !it.isJsonNull }?.asString,
                        totalMessages = data.get("total_messages")?.takeIf { !it.isJsonNull }?.asInt,
                        pipelineState = ps
                    )
                }
                "stream_error" -> WsEvent.StreamError(
                    detail = data.get("detail").asString
                )
                "branch_switched" -> WsEvent.BranchSwitched(
                    newLeafId = data.get("new_leaf_id").asString,
                    targetMessageId = data.get("target_message_id")?.takeIf { !it.isJsonNull }?.asString
                )
                "chat_settings_changed" -> WsEvent.ChatSettingsChanged(
                    model = data.get("model")?.takeIf { !it.isJsonNull }?.asString,
                    anthropicSync = data.get("anthropic_sync")?.takeIf { !it.isJsonNull }?.asBoolean
                )
                "bookmark_updated" -> WsEvent.BookmarkUpdated(
                    messageId = data.get("message_id").asString,
                    bookmark = data.get("bookmark")?.takeIf { !it.isJsonNull }?.asString
                )
                "state_update" -> {
                    val ps = gson.fromJson(data.get("pipeline_state"), PipelineState::class.java)
                    if (ps != null) WsEvent.StateUpdate(pipelineState = ps) else null
                }
                "pong" -> null
                else -> null
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to parse chat WS event: $text", e)
            null
        }
    }
}
