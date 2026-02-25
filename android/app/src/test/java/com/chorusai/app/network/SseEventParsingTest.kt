package com.chorusai.app.network

import com.chorusai.app.model.PipelineState
import com.chorusai.app.model.SseEvent
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonElement
import com.google.gson.reflect.TypeToken
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Tests the SSE event parsing logic extracted from SseClient.
 * We replicate the parseEvent logic here since it's private in SseClient.
 */
class SseEventParsingTest {

    private lateinit var gson: Gson

    @Before
    fun setup() {
        gson = GsonBuilder().create()
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
                    SseEvent.StateUpdate(data = ps)
                }
                "state_notifications" -> {
                    val map: Map<String, Any> = gson.fromJson(data, mapType)
                    val notifications = (map["notifications"] as? List<*>)?.filterIsInstance<String>() ?: emptyList()
                    SseEvent.StateNotifications(notifications = notifications)
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

    // ---- init event ----

    @Test
    fun `parse init event with user_message_id`() {
        val event = parseEvent("init", """{"user_message_id":"msg-123"}""")
        assertTrue(event is SseEvent.Init)
        assertEquals("msg-123", (event as SseEvent.Init).userMessageId)
    }

    @Test
    fun `parse init event without user_message_id`() {
        val event = parseEvent("init", """{}""")
        assertTrue(event is SseEvent.Init)
        assertNull((event as SseEvent.Init).userMessageId)
    }

    // ---- content event ----

    @Test
    fun `parse content event`() {
        val event = parseEvent("content", """{"delta":"Hello "}""")
        assertTrue(event is SseEvent.Content)
        assertEquals("Hello ", (event as SseEvent.Content).delta)
    }

    @Test
    fun `parse content event with empty delta`() {
        val event = parseEvent("content", """{}""")
        assertTrue(event is SseEvent.Content)
        assertEquals("", (event as SseEvent.Content).delta)
    }

    @Test
    fun `parse content event with special characters`() {
        val event = parseEvent("content", """{"delta":"Hello\nWorld \"quote\""}""")
        assertTrue(event is SseEvent.Content)
        assertEquals("Hello\nWorld \"quote\"", (event as SseEvent.Content).delta)
    }

    // ---- thinking event ----

    @Test
    fun `parse thinking event`() {
        val event = parseEvent("thinking", """{"delta":"Let me think..."}""")
        assertTrue(event is SseEvent.Thinking)
        assertEquals("Let me think...", (event as SseEvent.Thinking).delta)
    }

    // ---- pipeline_stage event ----

    @Test
    fun `parse pipeline_stage event`() {
        val event = parseEvent("pipeline_stage", """{"stage":"events","status":"running"}""")
        assertTrue(event is SseEvent.PipelineStage)
        val ps = event as SseEvent.PipelineStage
        assertEquals("events", ps.stage)
        assertEquals("running", ps.status)
    }

    @Test
    fun `parse pipeline_stage event with completed status`() {
        val event = parseEvent("pipeline_stage", """{"stage":"narration","status":"completed"}""")
        assertTrue(event is SseEvent.PipelineStage)
        assertEquals("narration", (event as SseEvent.PipelineStage).stage)
        assertEquals("completed", event.status)
    }

    // ---- state_update event ----

    @Test
    fun `parse state_update event`() {
        val event = parseEvent("state_update", """{"turn_counter":5,"pacing":{"episode":1}}""")
        assertTrue(event is SseEvent.StateUpdate)
        val ps = (event as SseEvent.StateUpdate).data
        assertEquals(5, ps.turnCounter)
    }

    // ---- state_notifications event ----

    @Test
    fun `parse state_notifications event`() {
        val event = parseEvent(
            "state_notifications",
            """{"notifications":["Combat started","Enemy appeared"]}"""
        )
        assertTrue(event is SseEvent.StateNotifications)
        val notifications = (event as SseEvent.StateNotifications).notifications
        assertEquals(2, notifications.size)
        assertEquals("Combat started", notifications[0])
        assertEquals("Enemy appeared", notifications[1])
    }

    @Test
    fun `parse state_notifications with empty list`() {
        val event = parseEvent("state_notifications", """{"notifications":[]}""")
        assertTrue(event is SseEvent.StateNotifications)
        assertTrue((event as SseEvent.StateNotifications).notifications.isEmpty())
    }

    // ---- docs_refreshed event ----

    @Test
    fun `parse docs_refreshed event`() {
        val event = parseEvent("docs_refreshed", """{}""")
        assertTrue(event is SseEvent.DocsRefreshed)
    }

    // ---- done event ----

    @Test
    fun `parse done event with all fields`() {
        val event = parseEvent("done", """{
            "tokens": "500",
            "cost": "0.01",
            "assistant_message_id": "a-1",
            "current_leaf_id": "leaf-1",
            "total_messages": 10,
            "model": "gpt-4"
        }""")
        assertTrue(event is SseEvent.Done)
        val done = event as SseEvent.Done
        assertEquals("500", done.tokens)
        assertEquals("0.01", done.cost)
        assertEquals("a-1", done.assistantMessageId)
        assertEquals("leaf-1", done.currentLeafId)
        assertEquals(10, done.totalMessages)
        assertEquals("gpt-4", done.model)
    }

    @Test
    fun `parse done event with minimal fields`() {
        val event = parseEvent("done", """{}""")
        assertTrue(event is SseEvent.Done)
        val done = event as SseEvent.Done
        assertNull(done.tokens)
        assertNull(done.cost)
        assertNull(done.model)
    }

    // ---- error event ----

    @Test
    fun `parse error event`() {
        val event = parseEvent("error", """{"detail":"Rate limit exceeded"}""")
        assertTrue(event is SseEvent.Error)
        assertEquals("Rate limit exceeded", (event as SseEvent.Error).detail)
    }

    @Test
    fun `parse error event without detail falls back to data`() {
        val event = parseEvent("error", """{"other":"field"}""")
        assertTrue(event is SseEvent.Error)
        // Falls back to the raw data string
        assertEquals("""{"other":"field"}""", (event as SseEvent.Error).detail)
    }

    // ---- unknown event ----

    @Test
    fun `parse unknown event type returns null`() {
        val event = parseEvent("unknown_type", """{"foo":"bar"}""")
        assertNull(event)
    }

    @Test
    fun `parse null event type returns null`() {
        val event = parseEvent(null, """{"foo":"bar"}""")
        assertNull(event)
    }

    // ---- malformed JSON ----

    @Test
    fun `malformed JSON returns Error event`() {
        val event = parseEvent("content", """not valid json""")
        assertTrue(event is SseEvent.Error)
        assertTrue((event as SseEvent.Error).detail.contains("Failed to parse SSE event"))
    }
}
