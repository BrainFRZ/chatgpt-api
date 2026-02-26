package com.chorusai.app.model

sealed class SseEvent {
    data class Init(
        val userMessageId: String? = null
    ) : SseEvent()

    data class Content(
        val delta: String
    ) : SseEvent()

    data class Thinking(
        val delta: String
    ) : SseEvent()

    data class PipelineStage(
        val stage: String,
        val status: String
    ) : SseEvent()

    data class StateUpdate(
        val data: PipelineState
    ) : SseEvent()

    data class StateNotifications(
        val notifications: List<String>
    ) : SseEvent()

    data class HackStateUpdate(val hackState: HackState) : SseEvent()

    data class ShipCombatAutoInit(val parentId: String?) : SseEvent()
    data class ShipCombatError(
        val detail: String
    ) : SseEvent()
    data class ShipCombatDone(
        val tokens: String? = null,
        val cost: String? = null,
        val stats: Map<String, Any>? = null,
        val userMessageId: String? = null,
        val assistantMessageId: String? = null,
        val shipCombatInitMessage: ChatMessage? = null,
        val currentLeafId: String? = null,
        val totalMessages: Int? = null,
        val model: String? = null,
        val reasoning: String? = null,
        val contextStartIndex: Int? = null
    ) : SseEvent()

    data object DocsRefreshed : SseEvent()

    data class Done(
        val tokens: String? = null,
        val cost: String? = null,
        val stats: Map<String, Any>? = null,
        val assistantMessageId: String? = null,
        val currentLeafId: String? = null,
        val totalMessages: Int? = null,
        val model: String? = null,
        val pipelineState: PipelineState? = null,
        val reasoning: String? = null,
        val contextStartIndex: Int? = null
    ) : SseEvent()

    data class Error(
        val detail: String
    ) : SseEvent()
}
