package com.chorusai.app.ui.viewmodel

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.ChatMessage
import com.chorusai.app.model.ModelInfo
import dagger.hilt.android.lifecycle.HiltViewModel
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

    fun sendMessage(content: String) {
        if (content.isBlank() || _uiState.value.isSending) return

        viewModelScope.launch {
            _uiState.update { it.copy(isSending = true, sendError = null) }
            val state = _uiState.value

            val tempUserId = "temp_user_${UUID.randomUUID()}"
            val tempAssistantId = "temp_assistant_${UUID.randomUUID()}"

            val userMessage = ChatMessage(
                id = tempUserId,
                role = "user",
                content = content,
                parentId = state.currentLeafId
            )
            val thinkingMessage = ChatMessage(
                id = tempAssistantId,
                role = "assistant",
                content = "Thinking..."
            )

            _uiState.update {
                it.copy(messages = it.messages + userMessage + thinkingMessage)
            }

            try {
                val response = chatRepo.sendMessage(
                    username = state.username,
                    chatName = state.chatName,
                    message = content,
                    project = state.project,
                    parentId = state.currentLeafId,
                    model = state.model
                )

                if (response.isSuccessful) {
                    val body = response.body()!!
                    val realUserMessage = userMessage.copy(id = body.userMessageId ?: tempUserId)
                    val realAssistantMessage = ChatMessage(
                        id = body.assistantMessageId ?: tempAssistantId,
                        role = "assistant",
                        content = body.assistantMessage,
                        tokens = body.tokens,
                        cost = body.cost,
                        model = body.model,
                        reasoning = body.reasoning
                    )

                    _uiState.update {
                        it.copy(
                            messages = it.messages.map { msg ->
                                when (msg.id) {
                                    tempUserId -> realUserMessage
                                    tempAssistantId -> realAssistantMessage
                                    else -> msg
                                }
                            },
                            currentLeafId = body.currentLeafId ?: it.currentLeafId,
                            totalMessages = body.totalMessages ?: it.totalMessages
                        )
                    }
                } else {
                    // Remove thinking placeholder, keep user message
                    _uiState.update {
                        it.copy(
                            messages = it.messages.filter { msg -> msg.id != tempAssistantId },
                            sendError = "Failed to send message"
                        )
                    }
                }
            } catch (e: Exception) {
                _uiState.update {
                    it.copy(
                        messages = it.messages.filter { msg -> msg.id != tempAssistantId },
                        sendError = e.message ?: "Network error"
                    )
                }
            } finally {
                _uiState.update { it.copy(isSending = false) }
            }
        }
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
