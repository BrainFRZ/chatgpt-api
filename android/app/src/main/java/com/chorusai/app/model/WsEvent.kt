package com.chorusai.app.model

sealed class WsEvent {
    // User-level events
    data class ChatCreated(val chatName: String, val project: String? = null) : WsEvent()
    data class ChatDeleted(val chatName: String, val project: String? = null) : WsEvent()

    // Chat-level events
    data class ClientJoined(val connectionCount: Int) : WsEvent()
    data class ClientLeft(val connectionCount: Int) : WsEvent()
    data class UserMessageAdded(val message: ChatMessage, val currentLeafId: String?) : WsEvent()
    data class StreamContent(val delta: String) : WsEvent()
    data class StreamThinking(val delta: String) : WsEvent()
    data class StreamDone(
        val assistantMessage: ChatMessage,
        val currentLeafId: String?,
        val totalMessages: Int?,
        val pipelineState: PipelineState? = null
    ) : WsEvent()
    data class StreamError(val detail: String) : WsEvent()
    data class BranchSwitched(val newLeafId: String, val targetMessageId: String?) : WsEvent()
    data class ChatSettingsChanged(val model: String?) : WsEvent()
    data class BookmarkUpdated(val messageId: String, val bookmark: String?) : WsEvent()
    data class StateUpdate(val pipelineState: PipelineState) : WsEvent()
}
