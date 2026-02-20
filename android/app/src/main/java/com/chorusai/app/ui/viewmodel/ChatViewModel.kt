package com.chorusai.app.ui.viewmodel

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.ChatMessage
import com.chorusai.app.model.ModelInfo
import com.chorusai.app.model.SseEvent
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
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
    val totalMessages: Int = 0,
    val hasMoreMessages: Boolean = false,
    val currentLeafId: String? = null,
    val model: String? = null,
    val isLoadingMore: Boolean = false,
    val isSending: Boolean = false,
    val isStreaming: Boolean = false,
    val streamingMessageId: String? = null,
    val sendError: String? = null,
    val models: List<ModelInfo> = emptyList(),
    val isLoadingModels: Boolean = false
)

sealed class ChatNavEvent {
    data object NavigateBack : ChatNavEvent()
}

@HiltViewModel
class ChatViewModel @Inject constructor(
    private val chatRepo: ChatRepository,
    private val prefs: UserPreferences,
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
            loadChat()
        }

        viewModelScope.launch {
            loadModels()
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
                _uiState.update {
                    it.copy(
                        messages = body.messages,
                        totalMessages = body.totalMessages,
                        hasMoreMessages = body.hasMoreMessages,
                        currentLeafId = body.currentLeafId,
                        model = body.model,
                        error = null
                    )
                }
            } else {
                _uiState.update { it.copy(error = "Failed to load chat") }
            }
        } catch (e: Exception) {
            _uiState.update { it.copy(error = e.message ?: "Network error") }
        } finally {
            _uiState.update { it.copy(isLoading = false) }
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
        if (content.isBlank() || _uiState.value.isSending) return

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
                        is SseEvent.Done -> {
                            val finalMessage = ChatMessage(
                                id = event.assistantMessageId ?: tempAssistantId,
                                role = "assistant",
                                content = accumulatedContent.toString(),
                                tokens = event.tokens,
                                cost = event.cost,
                                model = event.model,
                                reasoning = accumulatedReasoning.toString().ifEmpty { null }
                            )
                            _uiState.update {
                                it.copy(
                                    messages = it.messages.map { msg ->
                                        if (msg.id == tempAssistantId) finalMessage else msg
                                    },
                                    currentLeafId = event.currentLeafId ?: it.currentLeafId,
                                    totalMessages = event.totalMessages ?: it.totalMessages,
                                    isStreaming = false,
                                    streamingMessageId = null,
                                    isSending = false
                                )
                            }
                        }
                        is SseEvent.Error -> {
                            _uiState.update {
                                it.copy(
                                    messages = if (accumulatedContent.isEmpty()) {
                                        it.messages.filter { msg -> msg.id != tempAssistantId }
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
                        else -> { /* Ignore pipeline_stage, state_update, etc. for now */ }
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _uiState.update {
                    it.copy(
                        messages = if (accumulatedContent.isEmpty()) {
                            it.messages.filter { msg -> msg.id != tempAssistantId }
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
        _uiState.update { it.copy(isSending = false, isStreaming = false, streamingMessageId = null) }
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
}
