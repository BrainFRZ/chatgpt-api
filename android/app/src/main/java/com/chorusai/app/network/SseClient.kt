package com.chorusai.app.network

import com.chorusai.app.model.SseEvent
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
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
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.StateUpdate(data = map)
                }
                "state_notifications" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    val notifications = (map["notifications"] as? List<*>)?.filterIsInstance<String>() ?: emptyList()
                    SseEvent.StateNotifications(notifications = notifications)
                }
                "docs_refreshed" -> SseEvent.DocsRefreshed
                "done" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    SseEvent.Done(
                        tokens = map["tokens"] as? String,
                        cost = map["cost"] as? String,
                        stats = map["stats"] as? Map<String, Any>,
                        assistantMessageId = map["assistant_message_id"] as? String,
                        currentLeafId = map["current_leaf_id"] as? String,
                        totalMessages = (map["total_messages"] as? Double)?.toInt(),
                        model = map["model"] as? String,
                        pipelineState = map["pipeline_state"] as? Map<String, Any>
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
