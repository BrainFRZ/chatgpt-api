package com.chorusai.app.data

import com.chorusai.app.model.*
import com.chorusai.app.network.ChorusApi
import com.chorusai.app.network.SseClient
import kotlinx.coroutines.flow.Flow
import retrofit2.Response
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class ChatRepository @Inject constructor(
    private val api: ChorusApi,
    private val sseClient: SseClient
) {
    suspend fun getChats(
        username: String,
        limit: Int = 20,
        offset: Int = 0
    ): Response<ChatListResponse> =
        api.getChats(username, limit, offset)

    suspend fun createChat(
        username: String,
        chatName: String,
        project: String? = null,
        model: String? = null
    ): Response<Map<String, String>> =
        api.createChat(CreateChatRequest(username, chatName, project, model))

    suspend fun getChat(
        username: String,
        chatName: String,
        project: String? = null,
        leafId: String? = null,
        limit: Int = 30,
        offset: Int = 0
    ): Response<ChatResponse> =
        api.getChat(username, chatName, project, leafId, limit, offset)

    suspend fun sendMessage(
        username: String,
        chatName: String,
        message: String,
        project: String? = null,
        parentId: String? = null,
        attachedFiles: List<AttachedFile>? = null,
        model: String? = null
    ): Response<MessageResponse> =
        api.sendMessage(
            SendMessageRequest(username, chatName, message, project, parentId, attachedFiles, model)
        )

    fun streamMessage(
        username: String,
        chatName: String,
        message: String,
        project: String? = null,
        parentId: String? = null,
        attachedFiles: List<Map<String, String>>? = null,
        model: String? = null
    ): Flow<SseEvent> {
        val body = buildMap<String, Any?> {
            put("username", username)
            put("chat_name", chatName)
            put("message", message)
            if (project != null) put("project", project)
            if (parentId != null) put("parent_id", parentId)
            if (attachedFiles != null) put("attached_files", attachedFiles)
            if (model != null) put("model", model)
        }
        return sseClient.streamMessage(body)
    }

    suspend fun renameChat(
        username: String,
        oldName: String,
        newName: String,
        project: String? = null
    ): Response<Map<String, String>> =
        api.renameChat(RenameChatRequest(username, oldName, newName, project))

    suspend fun deleteChat(
        username: String,
        chatName: String,
        project: String? = null
    ): Response<Map<String, String>> =
        api.deleteChat(DeleteChatRequest(username, chatName, project))

    suspend fun getBranchInfo(
        username: String,
        chatName: String,
        messageId: String,
        project: String? = null
    ): Response<BranchInfoResponse> =
        api.getBranchInfo(username, chatName, messageId, project)

    suspend fun switchBranch(
        username: String,
        chatName: String,
        targetMessageId: String,
        project: String? = null
    ): Response<Map<String, String>> =
        api.switchBranch(username, chatName, targetMessageId, project)

    suspend fun setBookmark(
        username: String,
        chatName: String,
        messageId: String,
        bookmark: String,
        project: String? = null
    ): Response<Map<String, Any>> =
        api.setBookmark(SetBookmarkRequest(username, chatName, messageId, bookmark, project))

    suspend fun getModels(): Response<List<ModelInfo>> =
        api.getModels()

    suspend fun setChatModel(
        username: String,
        chatName: String,
        project: String? = null,
        model: String
    ): Response<Map<String, String>> =
        api.setChatModel(SetChatModelRequest(username, chatName, project, model))
}
