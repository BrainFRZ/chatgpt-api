package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class AttachedFile(
    val filename: String,
    val content: String
)

data class ChatMessage(
    val id: String? = null,
    @SerializedName("parent_id") val parentId: String? = null,
    val role: String,
    val content: String,
    val timestamp: String? = null,
    val tokens: String? = null,
    val cost: String? = null,
    val reasoning: String? = null,
    @SerializedName("total_tokens") val totalTokens: Int? = null,
    @SerializedName("total_gpt_tokens") val totalGptTokens: Int? = null,
    @SerializedName("total_claude_tokens") val totalClaudeTokens: Int? = null,
    @SerializedName("attached_files") val attachedFiles: List<AttachedFile>? = null,
    val model: String? = null,
    @SerializedName("service_tier") val serviceTier: String? = null,
    val bookmark: String? = null,
    @SerializedName("events_stage") val eventsStage: String? = null,
    @SerializedName("mechanics_stage") val mechanicsStage: String? = null
)

data class ChatListResponse(
    val chats: List<String>,
    val projects: List<String>,
    val total: Int,
    @SerializedName("has_more") val hasMore: Boolean
)

data class CreateChatRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    val project: String? = null,
    val model: String? = null
)

data class ChatResponse(
    val messages: List<ChatMessage>,
    @SerializedName("all_messages") val allMessages: List<ChatMessage>? = null,
    val stats: Map<String, Any>? = null,
    @SerializedName("total_messages") val totalMessages: Int,
    @SerializedName("has_more_messages") val hasMoreMessages: Boolean,
    @SerializedName("current_leaf_id") val currentLeafId: String? = null,
    val model: String? = null,
    @SerializedName("anthropic_sync") val anthropicSync: Boolean? = null,
    @SerializedName("pipeline_state") val pipelineState: Map<String, Any>? = null,
    @SerializedName("game_system") val gameSystem: String? = null
)

data class SendMessageRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    val message: String,
    val project: String? = null,
    @SerializedName("parent_id") val parentId: String? = null,
    @SerializedName("attached_files") val attachedFiles: List<AttachedFile>? = null,
    val model: String? = null
)

data class MessageResponse(
    @SerializedName("assistant_message") val assistantMessage: String,
    val tokens: String,
    val cost: String,
    val stats: Map<String, Any>? = null,
    @SerializedName("context_start_index") val contextStartIndex: Int,
    val reasoning: String? = null,
    @SerializedName("user_message_id") val userMessageId: String? = null,
    @SerializedName("assistant_message_id") val assistantMessageId: String? = null,
    @SerializedName("current_leaf_id") val currentLeafId: String? = null,
    @SerializedName("total_messages") val totalMessages: Int? = null,
    val model: String? = null
)

data class ChatSummary(
    val name: String,
    @SerializedName("last_message") val lastMessage: String,
    @SerializedName("last_active") val lastActive: String,
    @SerializedName("message_count") val messageCount: Int,
    val cost: Double
)

data class RenameChatRequest(
    val username: String,
    @SerializedName("old_name") val oldName: String,
    @SerializedName("new_name") val newName: String,
    val project: String? = null
)

data class DeleteChatRequest(
    val username: String,
    @SerializedName("chat_name") val chatName: String,
    val project: String? = null
)
