package com.chorusai.app.ui.viewmodel

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.ChatMessage
import com.chorusai.app.model.ModelInfo
import com.chorusai.app.model.PipelineState
import com.chorusai.app.model.SseEvent
import com.chorusai.app.model.WsEvent
import com.chorusai.app.network.WebSocketManager
import com.google.gson.Gson
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.util.UUID
import javax.inject.Inject

data class ChatUiState(
    val isLoading: Boolean = true,
    val error: String? = null,
    val chatName: String = "",
    val project: String? = null,
    val username: String = "",
    val messages: List<ChatMessage> = emptyList(),
    val allMessages: List<ChatMessage> = emptyList(),
    val totalMessages: Int = 0,
    val hasMoreMessages: Boolean = false,
    val currentLeafId: String? = null,
    val contextStartIndex: Int = 1,
    val model: String? = null,
    val isLoadingMore: Boolean = false,
    val isSending: Boolean = false,
    val isStreaming: Boolean = false,
    val streamingMessageId: String? = null,
    val sendError: String? = null,
    val models: List<ModelInfo> = emptyList(),
    val isLoadingModels: Boolean = false,
    val viewerCount: Int = 1,
    val isRemoteStreaming: Boolean = false,
    val chatDeleted: Boolean = false,
    val editingMessageId: String? = null,
    val editingMessageContent: String = "",
    val isSwitchingBranch: Boolean = false,
    val scrollToBottomTrigger: Int = 0,
    val pipelineState: PipelineState? = null,
    val gameSystem: String? = null,
    val characterSheetMd: String? = null
)

sealed class ChatNavEvent {
    data object NavigateBack : ChatNavEvent()
}

@HiltViewModel
class ChatViewModel @Inject constructor(
    private val chatRepo: ChatRepository,
    private val prefs: UserPreferences,
    private val webSocketManager: WebSocketManager,
    private val gson: Gson,
    savedStateHandle: SavedStateHandle
) : ViewModel() {

    private val _uiState = MutableStateFlow(ChatUiState())
    val uiState: StateFlow<ChatUiState> = _uiState.asStateFlow()

    private val _navEvents = Channel<ChatNavEvent>(Channel.BUFFERED)
    val navEvents = _navEvents.receiveAsFlow()

    private val pageSize = 30

    init {
        val chatName = savedStateHandle.get<String>("chatName") ?: ""
        val project = savedStateHandle.get<String>("project")
        _uiState.update { it.copy(chatName = chatName, project = project) }

        viewModelScope.launch {
            val username = prefs.username.first() ?: ""
            _uiState.update { it.copy(username = username) }
            webSocketManager.connectChat(username, chatName, project)
            loadChat()
        }

        viewModelScope.launch {
            loadModels()
        }

        viewModelScope.launch {
            webSocketManager.chatEvents.collect { event ->
                handleChatWsEvent(event)
            }
        }

        viewModelScope.launch {
            webSocketManager.userEvents.collect { event ->
                if (event is WsEvent.ChatDeleted) {
                    val state = _uiState.value
                    if (event.chatName == state.chatName &&
                        (event.project ?: "") == (state.project ?: "")
                    ) {
                        _uiState.update { it.copy(chatDeleted = true) }
                    }
                }
            }
        }
    }

    private suspend fun loadChat() {
        val state = _uiState.value
        if (state.username.isBlank() || state.chatName.isBlank()) {
            _uiState.update { it.copy(isLoading = false) }
            return
        }
        try {
            val response = chatRepo.getChat(
                username = state.username,
                chatName = state.chatName,
                project = state.project,
                limit = pageSize,
                offset = 0
            )
            if (response.isSuccessful) {
                val body = response.body()!!
                val ps = body.pipelineState?.let { rawMap ->
                    try {
                        val json = gson.toJson(rawMap)
                        gson.fromJson(json, PipelineState::class.java)
                    } catch (_: Exception) { null }
                }
                _uiState.update {
                    it.copy(
                        messages = body.messages,
                        allMessages = body.allMessages ?: body.messages,
                        totalMessages = body.totalMessages,
                        hasMoreMessages = body.hasMoreMessages,
                        currentLeafId = body.currentLeafId,
                        contextStartIndex = 1,
                        model = body.model,
                        error = null,
                        pipelineState = ps,
                        gameSystem = body.gameSystem
                    )
                }
            } else {
                _uiState.update { it.copy(error = "Failed to load chat") }
            }
        } catch (e: Exception) {
            _uiState.update { it.copy(error = e.message ?: "Network error") }
        } finally {
            _uiState.update { it.copy(isLoading = false, editingMessageId = null, editingMessageContent = "") }
        }
    }

    fun loadMore() {
        if (_uiState.value.isLoadingMore || !_uiState.value.hasMoreMessages) return
        viewModelScope.launch {
            _uiState.update { it.copy(isLoadingMore = true) }
            val state = _uiState.value
            try {
                val response = chatRepo.getChat(
                    username = state.username,
                    chatName = state.chatName,
                    project = state.project,
                    leafId = state.currentLeafId,
                    limit = pageSize,
                    offset = state.messages.size
                )
                if (response.isSuccessful) {
                    val body = response.body()!!
                    _uiState.update {
                        it.copy(
                            messages = body.messages + it.messages,
                            totalMessages = body.totalMessages,
                            hasMoreMessages = body.hasMoreMessages
                        )
                    }
                }
            } catch (_: Exception) {
                // Silent fail for pagination
            } finally {
                _uiState.update { it.copy(isLoadingMore = false) }
            }
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, error = null) }
            loadChat()
        }
    }

    fun navigateBack() {
        viewModelScope.launch {
            _navEvents.send(ChatNavEvent.NavigateBack)
        }
    }

    private suspend fun loadModels() {
        _uiState.update { it.copy(isLoadingModels = true) }
        try {
            val response = chatRepo.getModels()
            if (response.isSuccessful) {
                _uiState.update { it.copy(models = response.body() ?: emptyList()) }
            }
        } catch (_: Exception) {
            // Silent fail — models are non-critical
        } finally {
            _uiState.update { it.copy(isLoadingModels = false) }
        }
    }

    private var streamingJob: Job? = null

    fun sendMessage(content: String) {
        if (content.isBlank() || _uiState.value.isSending || _uiState.value.isRemoteStreaming) return

        streamingJob = viewModelScope.launch {
            _uiState.update { it.copy(isSending = true, isStreaming = false, sendError = null) }
            val state = _uiState.value

            val tempUserId = "temp_user_${UUID.randomUUID()}"
            val tempAssistantId = "temp_assistant_${UUID.randomUUID()}"

            val userMessage = ChatMessage(
                id = tempUserId,
                role = "user",
                content = content,
                parentId = state.currentLeafId
            )
            val streamingMessage = ChatMessage(
                id = tempAssistantId,
                role = "assistant",
                content = ""
            )

            _uiState.update {
                it.copy(
                    messages = it.messages + userMessage + streamingMessage,
                    isStreaming = true,
                    streamingMessageId = tempAssistantId
                )
            }

            val accumulatedContent = StringBuilder()
            val accumulatedReasoning = StringBuilder()

            try {
                chatRepo.streamMessage(
                    username = state.username,
                    chatName = state.chatName,
                    message = content,
                    project = state.project,
                    parentId = state.currentLeafId,
                    model = state.model
                ).collect { event ->
                    when (event) {
                        is SseEvent.Init -> {
                            val newUserId = event.userMessageId
                            if (newUserId != null) {
                                _uiState.update {
                                    it.copy(messages = it.messages.map { msg ->
                                        if (msg.id == tempUserId) msg.copy(id = newUserId) else msg
                                    })
                                }
                            }
                        }
                        is SseEvent.Content -> {
                            accumulatedContent.append(event.delta)
                            val newContent = accumulatedContent.toString()
                            _uiState.update {
                                it.copy(messages = it.messages.map { msg ->
                                    if (msg.id == tempAssistantId) msg.copy(content = newContent) else msg
                                })
                            }
                        }
                        is SseEvent.Thinking -> {
                            accumulatedReasoning.append(event.delta)
                            val newReasoning = accumulatedReasoning.toString()
                            _uiState.update {
                                it.copy(messages = it.messages.map { msg ->
                                    if (msg.id == tempAssistantId) msg.copy(reasoning = newReasoning) else msg
                                })
                            }
                        }
                        is SseEvent.StateUpdate -> {
                            _uiState.update { it.copy(pipelineState = event.data) }
                        }
                        is SseEvent.Done -> {
                            val finalMessage = ChatMessage(
                                id = event.assistantMessageId ?: tempAssistantId,
                                role = "assistant",
                                content = accumulatedContent.toString(),
                                tokens = event.tokens,
                                cost = event.cost,
                                model = event.model,
                                reasoning = accumulatedReasoning.toString().ifEmpty { null }
                                    ?: event.reasoning
                            )
                            _uiState.update {
                                it.copy(
                                    messages = it.messages.map { msg ->
                                        if (msg.id == tempAssistantId) finalMessage else msg
                                    },
                                    currentLeafId = event.currentLeafId ?: it.currentLeafId,
                                    totalMessages = event.totalMessages ?: it.totalMessages,
                                    contextStartIndex = event.contextStartIndex ?: it.contextStartIndex,
                                    isStreaming = false,
                                    streamingMessageId = null,
                                    isSending = false,
                                    pipelineState = event.pipelineState ?: it.pipelineState
                                )
                            }
                            // Reload allMessages to include the new messages
                            reloadAllMessages()
                        }
                        is SseEvent.Error -> {
                            _uiState.update {
                                it.copy(
                                    messages = if (accumulatedContent.isEmpty()) {
                                        it.messages.filter { msg ->
                                            msg.id != tempAssistantId && msg.id != tempUserId
                                        }
                                    } else {
                                        it.messages
                                    },
                                    sendError = event.detail,
                                    isStreaming = false,
                                    streamingMessageId = null,
                                    isSending = false
                                )
                            }
                        }
                        else -> { /* Ignore pipeline_stage, etc. */ }
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _uiState.update {
                    it.copy(
                        messages = if (accumulatedContent.isEmpty()) {
                            it.messages.filter { msg ->
                                msg.id != tempAssistantId && msg.id != tempUserId
                            }
                        } else {
                            it.messages
                        },
                        sendError = e.message ?: "Streaming error",
                        isStreaming = false,
                        streamingMessageId = null,
                        isSending = false
                    )
                }
            }
            // Ensure flags are reset if flow completed without done/error (e.g. stream closed early)
            if (_uiState.value.isSending) {
                _uiState.update { it.copy(isSending = false, isStreaming = false, streamingMessageId = null) }
            }
        }
    }

    fun cancelStreaming() {
        streamingJob?.cancel()
        streamingJob = null
        clearRemoteStreamState()
        _uiState.update {
            it.copy(
                isSending = false,
                isStreaming = false,
                isRemoteStreaming = false,
                streamingMessageId = null
            )
        }
        // Reload to recover correct state (especially after edit truncation)
        viewModelScope.launch { loadChat() }
    }

    fun setModel(modelId: String) {
        val previousModel = _uiState.value.model
        _uiState.update { it.copy(model = modelId) }

        viewModelScope.launch {
            val state = _uiState.value
            try {
                val response = chatRepo.setChatModel(
                    username = state.username,
                    chatName = state.chatName,
                    project = state.project,
                    model = modelId
                )
                if (!response.isSuccessful) {
                    _uiState.update { it.copy(model = previousModel) }
                }
            } catch (_: Exception) {
                _uiState.update { it.copy(model = previousModel) }
            }
        }
    }

    fun clearSendError() {
        _uiState.update { it.copy(sendError = null) }
    }

    // --- Tree utilities ---

    /**
     * Find all siblings of a message (messages sharing the same parent_id).
     * Returns them sorted by timestamp (oldest first), mirroring the web app.
     */
    fun getSiblings(messageId: String): List<ChatMessage> {
        val all = _uiState.value.allMessages
        val target = all.find { it.id == messageId } ?: return emptyList()
        val parentId = target.parentId
        return all.filter { it.parentId == parentId && it.role != "system" }
            .sortedBy { it.timestamp ?: "" }
    }

    // --- Branch switching ---

    fun switchBranch(targetMessageId: String) {
        if (_uiState.value.isSwitchingBranch) return
        viewModelScope.launch {
            _uiState.update { it.copy(isSwitchingBranch = true) }
            val state = _uiState.value
            try {
                val response = chatRepo.switchBranch(
                    username = state.username,
                    chatName = state.chatName,
                    targetMessageId = targetMessageId,
                    project = state.project
                )
                if (response.isSuccessful) {
                    val newLeafId = response.body()?.get("new_leaf_id")
                    // Reload the chat at the new leaf
                    val chatResponse = chatRepo.getChat(
                        username = state.username,
                        chatName = state.chatName,
                        project = state.project,
                        leafId = newLeafId,
                        limit = pageSize,
                        offset = 0
                    )
                    if (chatResponse.isSuccessful) {
                        val body = chatResponse.body()!!
                        val ps = body.pipelineState?.let { rawMap ->
                            try {
                                val json = gson.toJson(rawMap)
                                gson.fromJson(json, PipelineState::class.java)
                            } catch (_: Exception) { null }
                        }
                        _uiState.update {
                            it.copy(
                                messages = body.messages,
                                allMessages = body.allMessages ?: body.messages,
                                totalMessages = body.totalMessages,
                                hasMoreMessages = body.hasMoreMessages,
                                currentLeafId = body.currentLeafId,
                                contextStartIndex = 1,
                                model = body.model,
                                isSwitchingBranch = false,
                                editingMessageId = null,
                                editingMessageContent = "",
                                scrollToBottomTrigger = it.scrollToBottomTrigger + 1,
                                pipelineState = ps,
                                gameSystem = body.gameSystem
                            )
                        }
                    } else {
                        _uiState.update { it.copy(isSwitchingBranch = false) }
                    }
                } else {
                    _uiState.update { it.copy(isSwitchingBranch = false) }
                }
            } catch (_: Exception) {
                _uiState.update { it.copy(isSwitchingBranch = false) }
            }
        }
    }

    // --- Message editing ---

    fun startEditMessage(messageId: String) {
        val messages = _uiState.value.messages.filter { it.role != "system" }
        val msg = messages.find { it.id == messageId } ?: return
        if (msg.role != "user") return
        _uiState.update {
            it.copy(editingMessageId = messageId, editingMessageContent = msg.content)
        }
    }

    fun updateEditContent(content: String) {
        _uiState.update { it.copy(editingMessageContent = content) }
    }

    fun cancelEditMessage() {
        _uiState.update { it.copy(editingMessageId = null, editingMessageContent = "") }
    }

    fun saveEditMessage() {
        val state = _uiState.value
        val editId = state.editingMessageId ?: return
        val editContent = state.editingMessageContent.trim()
        if (editContent.isBlank() || state.isSending || state.isRemoteStreaming) return

        val visibleMessages = state.messages.filter { it.role != "system" }
        val editIndex = visibleMessages.indexOfFirst { it.id == editId }
        if (editIndex < 0) return
        val originalMessage = visibleMessages[editIndex]

        // Cancel editing state first
        _uiState.update { it.copy(editingMessageId = null, editingMessageContent = "") }

        // The parent_id for the new branch = the original message's parent_id
        // This creates a sibling of the original message
        val parentId = originalMessage.parentId

        streamingJob = viewModelScope.launch {
            _uiState.update { it.copy(isSending = true, isStreaming = false, sendError = null) }

            val tempUserId = "temp_user_${UUID.randomUUID()}"
            val tempAssistantId = "temp_assistant_${UUID.randomUUID()}"

            val userMessage = ChatMessage(
                id = tempUserId,
                role = "user",
                content = editContent,
                parentId = parentId
            )
            val streamingMessage = ChatMessage(
                id = tempAssistantId,
                role = "assistant",
                content = ""
            )

            // Replace everything from editIndex onward with the new user message + placeholder
            // Preserve any system messages from the original list
            val systemMessages = state.messages.filter { it.role == "system" }
            val prefix = visibleMessages.subList(0, editIndex)
            _uiState.update {
                it.copy(
                    messages = systemMessages + prefix + userMessage + streamingMessage,
                    isStreaming = true,
                    streamingMessageId = tempAssistantId
                )
            }

            val accumulatedContent = StringBuilder()
            val accumulatedReasoning = StringBuilder()

            try {
                chatRepo.streamMessage(
                    username = state.username,
                    chatName = state.chatName,
                    message = editContent,
                    project = state.project,
                    parentId = parentId,
                    model = state.model
                ).collect { event ->
                    when (event) {
                        is SseEvent.Init -> {
                            val newUserId = event.userMessageId
                            if (newUserId != null) {
                                _uiState.update {
                                    it.copy(messages = it.messages.map { msg ->
                                        if (msg.id == tempUserId) msg.copy(id = newUserId) else msg
                                    })
                                }
                            }
                        }
                        is SseEvent.Content -> {
                            accumulatedContent.append(event.delta)
                            val newContent = accumulatedContent.toString()
                            _uiState.update {
                                it.copy(messages = it.messages.map { msg ->
                                    if (msg.id == tempAssistantId) msg.copy(content = newContent) else msg
                                })
                            }
                        }
                        is SseEvent.Thinking -> {
                            accumulatedReasoning.append(event.delta)
                            val newReasoning = accumulatedReasoning.toString()
                            _uiState.update {
                                it.copy(messages = it.messages.map { msg ->
                                    if (msg.id == tempAssistantId) msg.copy(reasoning = newReasoning) else msg
                                })
                            }
                        }
                        is SseEvent.StateUpdate -> {
                            _uiState.update { it.copy(pipelineState = event.data) }
                        }
                        is SseEvent.Done -> {
                            val finalMessage = ChatMessage(
                                id = event.assistantMessageId ?: tempAssistantId,
                                role = "assistant",
                                content = accumulatedContent.toString(),
                                tokens = event.tokens,
                                cost = event.cost,
                                model = event.model,
                                reasoning = accumulatedReasoning.toString().ifEmpty { null }
                                    ?: event.reasoning
                            )
                            _uiState.update {
                                it.copy(
                                    messages = it.messages.map { msg ->
                                        if (msg.id == tempAssistantId) finalMessage else msg
                                    },
                                    currentLeafId = event.currentLeafId ?: it.currentLeafId,
                                    totalMessages = event.totalMessages ?: it.totalMessages,
                                    contextStartIndex = event.contextStartIndex ?: it.contextStartIndex,
                                    isStreaming = false,
                                    streamingMessageId = null,
                                    isSending = false,
                                    pipelineState = event.pipelineState ?: it.pipelineState
                                )
                            }
                            // Reload allMessages to include the new branch
                            reloadAllMessages()
                        }
                        is SseEvent.Error -> {
                            _uiState.update {
                                it.copy(
                                    sendError = event.detail,
                                    isStreaming = false,
                                    streamingMessageId = null,
                                    isSending = false
                                )
                            }
                            // Reload to restore correct state
                            loadChat()
                        }
                        else -> {}
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _uiState.update {
                    it.copy(
                        sendError = e.message ?: "Streaming error",
                        isStreaming = false,
                        streamingMessageId = null,
                        isSending = false
                    )
                }
                loadChat()
            }
            if (_uiState.value.isSending) {
                _uiState.update { it.copy(isSending = false, isStreaming = false, streamingMessageId = null) }
            }
        }
    }

    /** Re-fetch allMessages without replacing the visible branch path */
    private suspend fun reloadAllMessages() {
        val state = _uiState.value
        try {
            val response = chatRepo.getChat(
                username = state.username,
                chatName = state.chatName,
                project = state.project,
                leafId = state.currentLeafId,
                limit = pageSize,
                offset = 0
            )
            if (response.isSuccessful) {
                val body = response.body()!!
                _uiState.update {
                    it.copy(allMessages = body.allMessages ?: body.messages)
                }
            }
        } catch (_: Exception) {
            // Silent fail — allMessages is non-critical for display
        }
    }

    override fun onCleared() {
        super.onCleared()
        webSocketManager.disconnectChat()
    }

    // Accumulated content/reasoning for remote streaming
    private val remoteContent = StringBuilder()
    private val remoteReasoning = StringBuilder()
    private var remoteStreamTimeoutJob: Job? = null

    private fun clearRemoteStreamState() {
        remoteStreamTimeoutJob?.cancel()
        remoteContent.clear()
        remoteReasoning.clear()
    }

    private fun handleChatWsEvent(event: WsEvent) {
        when (event) {
            is WsEvent.ClientJoined -> {
                // If we were mid-remote-stream, the WS reconnected — clean up and reload
                if (_uiState.value.isRemoteStreaming) {
                    val orphanedId = _uiState.value.streamingMessageId
                    clearRemoteStreamState()
                    _uiState.update {
                        it.copy(
                            messages = it.messages.filter { msg -> msg.id != orphanedId },
                            isRemoteStreaming = false,
                            streamingMessageId = null,
                            viewerCount = event.connectionCount
                        )
                    }
                    viewModelScope.launch { loadChat() }
                } else {
                    _uiState.update { it.copy(viewerCount = event.connectionCount) }
                }
            }
            is WsEvent.ClientLeft -> {
                _uiState.update { it.copy(viewerCount = event.connectionCount) }
            }
            is WsEvent.UserMessageAdded -> {
                if (_uiState.value.isSending) return // Skip if we're the sender
                val tempId = "remote_assistant_${UUID.randomUUID()}"
                val placeholder = ChatMessage(id = tempId, role = "assistant", content = "")
                clearRemoteStreamState()
                _uiState.update {
                    val currentMessages = it.messages
                    val parentId = event.message.parentId

                    // If the parent is in our path but not the last message, this is
                    // a branch (edit). Truncate to the parent before appending.
                    val parentIndex = if (parentId != null) {
                        currentMessages.indexOfFirst { msg -> msg.id == parentId }
                    } else -1

                    val newMessages = if (parentIndex >= 0 && parentIndex < currentMessages.lastIndex) {
                        // Branch: truncate to parent, then append new message + placeholder
                        currentMessages.subList(0, parentIndex + 1) + event.message + placeholder
                    } else if (parentId != null && parentIndex == -1) {
                        // Parent not in visible path (e.g. system/root msg) — find sibling
                        val siblingIndex = currentMessages.indexOfFirst { msg -> msg.parentId == parentId }
                        if (siblingIndex >= 0) {
                            currentMessages.subList(0, siblingIndex) + event.message + placeholder
                        } else {
                            currentMessages + event.message + placeholder
                        }
                    } else {
                        // Normal append
                        currentMessages + event.message + placeholder
                    }

                    it.copy(
                        messages = newMessages,
                        allMessages = it.allMessages + event.message,
                        currentLeafId = event.currentLeafId ?: it.currentLeafId,
                        isRemoteStreaming = true,
                        streamingMessageId = tempId
                    )
                }
                // Safety timeout — if no StreamDone/StreamError arrives, clean up and reload
                remoteStreamTimeoutJob = viewModelScope.launch {
                    delay(120_000L)
                    if (_uiState.value.isRemoteStreaming) {
                        val orphanedId = _uiState.value.streamingMessageId
                        remoteContent.clear()
                        remoteReasoning.clear()
                        _uiState.update {
                            it.copy(
                                messages = it.messages.filter { msg -> msg.id != orphanedId },
                                isRemoteStreaming = false,
                                streamingMessageId = null
                            )
                        }
                        loadChat()
                    }
                }
            }
            is WsEvent.StreamContent -> {
                if (_uiState.value.isSending) return
                val streamId = _uiState.value.streamingMessageId ?: return
                remoteContent.append(event.delta)
                val newContent = remoteContent.toString()
                _uiState.update {
                    it.copy(messages = it.messages.map { msg ->
                        if (msg.id == streamId) msg.copy(content = newContent) else msg
                    })
                }
            }
            is WsEvent.StreamThinking -> {
                if (_uiState.value.isSending) return
                val streamId = _uiState.value.streamingMessageId ?: return
                remoteReasoning.append(event.delta)
                val newReasoning = remoteReasoning.toString()
                _uiState.update {
                    it.copy(messages = it.messages.map { msg ->
                        if (msg.id == streamId) msg.copy(reasoning = newReasoning) else msg
                    })
                }
            }
            is WsEvent.StreamDone -> {
                if (_uiState.value.isSending) return
                val streamId = _uiState.value.streamingMessageId ?: return
                clearRemoteStreamState()
                _uiState.update {
                    it.copy(
                        messages = it.messages.map { msg ->
                            if (msg.id == streamId) event.assistantMessage else msg
                        },
                        allMessages = it.allMessages + event.assistantMessage,
                        currentLeafId = event.currentLeafId ?: it.currentLeafId,
                        totalMessages = event.totalMessages ?: it.totalMessages,
                        pipelineState = event.pipelineState ?: it.pipelineState,
                        isRemoteStreaming = false,
                        streamingMessageId = null
                    )
                }
            }
            is WsEvent.StreamError -> {
                if (_uiState.value.isSending) return
                val streamId = _uiState.value.streamingMessageId ?: return
                val hadContent = remoteContent.isNotEmpty()
                clearRemoteStreamState()
                _uiState.update {
                    it.copy(
                        messages = if (!hadContent) {
                            it.messages.filter { msg -> msg.id != streamId }
                        } else {
                            it.messages
                        },
                        isRemoteStreaming = false,
                        streamingMessageId = null
                    )
                }
            }
            is WsEvent.ChatSettingsChanged -> {
                event.model?.let { model ->
                    _uiState.update { it.copy(model = model) }
                }
            }
            is WsEvent.BranchSwitched -> {
                // Skip if we triggered this switch locally (avoid redundant reload + flash)
                if (_uiState.value.isSwitchingBranch) return
                viewModelScope.launch {
                    _uiState.update { it.copy(isLoading = true) }
                    loadChat()
                    _uiState.update { it.copy(scrollToBottomTrigger = it.scrollToBottomTrigger + 1) }
                }
            }
            is WsEvent.BookmarkUpdated -> {
                _uiState.update {
                    it.copy(messages = it.messages.map { msg ->
                        if (msg.id == event.messageId) msg.copy(bookmark = event.bookmark) else msg
                    })
                }
            }
            is WsEvent.StateUpdate -> {
                _uiState.update { it.copy(pipelineState = event.pipelineState) }
            }
            else -> { /* Ignore user-level events in chat context */ }
        }
    }

    fun fetchCharacterSheet() {
        val state = _uiState.value
        if (state.characterSheetMd != null || state.project == null) return
        viewModelScope.launch {
            try {
                val response = chatRepo.getCharacterSheet(state.username, state.project)
                if (response.isSuccessful) {
                    val md = response.body()?.get("content") ?: response.body()?.get("sheet") ?: ""
                    _uiState.update { it.copy(characterSheetMd = md) }
                }
            } catch (_: Exception) {
                // Silent fail — character sheet is non-critical
            }
        }
    }
}
