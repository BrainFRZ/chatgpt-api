package com.chorusai.app.network

import com.chorusai.app.model.HackState
import com.chorusai.app.model.PipelineState
import com.chorusai.app.model.SseEvent
import com.google.gson.Gson
import com.google.gson.JsonElement
import com.google.gson.reflect.TypeToken
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.logging.HttpLoggingInterceptor
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import java.util.concurrent.TimeUnit

class SseClient(
    okHttpClient: OkHttpClient,
    private val gson: Gson,
    private val baseUrl: String
) {
    private val sseHttpClient = okHttpClient.newBuilder()
        .readTimeout(10, TimeUnit.MINUTES)
        .apply {
            // Downgrade BODY-level logging to HEADERS for SSE — BODY buffers the
            // entire response before passing it through, breaking token-by-token streaming
            val interceptorsToReplace = interceptors().filterIsInstance<HttpLoggingInterceptor>()
            interceptorsToReplace.forEach { interceptors().remove(it) }
            if (interceptorsToReplace.isNotEmpty()) {
                addInterceptor(HttpLoggingInterceptor().apply {
                    level = HttpLoggingInterceptor.Level.HEADERS
                })
            }
        }
        .build()

    fun streamMessage(requestBody: Map<String, Any?>): Flow<SseEvent> = callbackFlow {
        val json = gson.toJson(requestBody)
        val body = json.toRequestBody("application/json".toMediaType())

        val request = Request.Builder()
            .url("${baseUrl}api/send-message-stream")
            .post(body)
            .header("Accept", "text/event-stream")
            .build()

        val listener = object : EventSourceListener() {
            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                val event = parseEvent(type, data)
                if (event != null) {
                    trySend(event)
                }
            }

            override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
                val message = t?.message ?: response?.message ?: "SSE connection failed"
                response?.close()
                trySend(SseEvent.Error(detail = message))
                close()
            }

            override fun onClosed(eventSource: EventSource) {
                close()
            }
        }

        val factory = EventSources.createFactory(sseHttpClient)
        val eventSource = factory.newEventSource(request, listener)

        awaitClose {
            eventSource.cancel()
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseEvent(type: String?, data: String): SseEvent? {
        val mapType = object : TypeToken<Map<String, Any>>() {}.type
        return try {
            when (type) {
                "init" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.Init(userMessageId = map["user_message_id"] as? String)
                }
                "content" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.Content(delta = map["delta"] as? String ?: "")
                }
                "thinking" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.Thinking(delta = map["delta"] as? String ?: "")
                }
                "pipeline_stage" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.PipelineStage(
                        stage = map["stage"] as? String ?: "",
                        status = map["status"] as? String ?: ""
                    )
                }
                "state_update" -> {
                    val ps = gson.fromJson(data, PipelineState::class.java)
                    if (ps != null) SseEvent.StateUpdate(data = ps) else null
                }
                "state_notifications" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    val notifications = (map["notifications"] as? List<*>)?.filterIsInstance<String>() ?: emptyList()
                    SseEvent.StateNotifications(notifications = notifications)
                }
                "hack_state_update", "hack_mode_start" -> {
                    val hs = gson.fromJson(data, HackState::class.java)
                    if (hs != null) SseEvent.HackStateUpdate(hackState = hs) else null
                }
                "ship_combat_auto_init" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.ShipCombatAutoInit(parentId = map["parent_id"] as? String)
                }
                "ship_combat_error" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.ShipCombatError(detail = map["detail"] as? String ?: data)
                }
                "ship_combat_done" -> {
                    val jsonEl = gson.fromJson(data, JsonElement::class.java)
                    val obj = jsonEl.asJsonObject
                    val shipCombatInitMessage = obj.get("ship_combat_init_message")?.takeIf { !it.isJsonNull }?.let {
                        gson.fromJson(it, com.chorusai.app.model.ChatMessage::class.java)
                    }
                    SseEvent.ShipCombatDone(
                        tokens = obj.get("tokens")?.takeIf { !it.isJsonNull }?.asString,
                        cost = obj.get("cost")?.takeIf { !it.isJsonNull }?.asString,
                        stats = obj.get("stats")?.takeIf { !it.isJsonNull }?.let {
                            gson.fromJson<Map<String, Any>>(it, mapType)
                        },
                        userMessageId = obj.get("user_message_id")?.takeIf { !it.isJsonNull }?.asString,
                        assistantMessageId = obj.get("assistant_message_id")?.takeIf { !it.isJsonNull }?.asString,
                        shipCombatInitMessage = shipCombatInitMessage,
                        currentLeafId = obj.get("current_leaf_id")?.takeIf { !it.isJsonNull }?.asString,
                        totalMessages = obj.get("total_messages")?.takeIf { !it.isJsonNull }?.asInt,
                        model = obj.get("model")?.takeIf { !it.isJsonNull }?.asString,
                        reasoning = obj.get("reasoning")?.takeIf { !it.isJsonNull }?.asString,
                        contextStartIndex = obj.get("context_start_index")?.takeIf { !it.isJsonNull }?.asInt
                    )
                }
                "docs_refreshed" -> SseEvent.DocsRefreshed
                "done" -> {
                    val jsonEl = gson.fromJson(data, JsonElement::class.java)
                    val obj = jsonEl.asJsonObject
                    val pipelineState = obj.get("pipeline_state")?.takeIf { !it.isJsonNull }?.let {
                        gson.fromJson(it, PipelineState::class.java)
                    }
                    SseEvent.Done(
                        tokens = obj.get("tokens")?.takeIf { !it.isJsonNull }?.asString,
                        cost = obj.get("cost")?.takeIf { !it.isJsonNull }?.asString,
                        stats = obj.get("stats")?.takeIf { !it.isJsonNull }?.let {
                            gson.fromJson<Map<String, Any>>(it, mapType)
                        },
                        assistantMessageId = obj.get("assistant_message_id")?.takeIf { !it.isJsonNull }?.asString,
                        currentLeafId = obj.get("current_leaf_id")?.takeIf { !it.isJsonNull }?.asString,
                        totalMessages = obj.get("total_messages")?.takeIf { !it.isJsonNull }?.asInt,
                        model = obj.get("model")?.takeIf { !it.isJsonNull }?.asString,
                        pipelineState = pipelineState,
                        reasoning = obj.get("reasoning")?.takeIf { !it.isJsonNull }?.asString,
                        contextStartIndex = obj.get("context_start_index")?.takeIf { !it.isJsonNull }?.asInt
                    )
                }
                "error" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.Error(detail = map["detail"] as? String ?: data)
                }
                else -> null
            }
        } catch (e: Exception) {
            SseEvent.Error(detail = "Failed to parse SSE event: ${e.message}")
        }
    }
}
