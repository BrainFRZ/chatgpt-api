package com.chorusai.app.ui.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.chorusai.app.data.AuthRepository
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.WsEvent
import com.chorusai.app.network.WebSocketManager
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class ChatListUiState(
    val isLoading: Boolean = true,
    val isRefreshing: Boolean = false,
    val error: String? = null,
    val username: String = "",
    val chats: List<String> = emptyList(),
    val projects: List<String> = emptyList(),
    val total: Int = 0,
    val hasMore: Boolean = false,
    val isLoadingMore: Boolean = false,
    val showCreateDialog: Boolean = false,
    val showDeleteDialog: String? = null,
    val showRenameDialog: String? = null,
    val dialogError: String? = null,
    val isDialogLoading: Boolean = false
)

sealed class ChatListNavEvent {
    data class NavigateToChat(val chatName: String, val project: String? = null) : ChatListNavEvent()
    data class NavigateToProject(val projectName: String) : ChatListNavEvent()
    data object NavigateToSettings : ChatListNavEvent()
    data object NavigateToLogin : ChatListNavEvent()
}

@HiltViewModel
class ChatListViewModel @Inject constructor(
    private val chatRepo: ChatRepository,
    private val authRepo: AuthRepository,
    private val prefs: UserPreferences,
    private val webSocketManager: WebSocketManager
) : ViewModel() {

    private val _uiState = MutableStateFlow(ChatListUiState())
    val uiState: StateFlow<ChatListUiState> = _uiState.asStateFlow()

    private val _navEvents = Channel<ChatListNavEvent>(Channel.BUFFERED)
    val navEvents = _navEvents.receiveAsFlow()

    private val pageSize = 20

    init {
        viewModelScope.launch {
            val username = prefs.username.first() ?: ""
            _uiState.update { it.copy(username = username) }
            if (username.isNotBlank()) {
                webSocketManager.connectUser(username)
            }
            loadChats()
        }

        viewModelScope.launch {
            webSocketManager.userEvents.collect { event ->
                when (event) {
                    is WsEvent.ChatCreated -> {
                        // Only handle non-project chats in the main chat list
                        if (event.project == null) {
                            _uiState.update {
                                if (event.chatName !in it.chats) {
                                    it.copy(
                                        chats = listOf(event.chatName) + it.chats,
                                        total = it.total + 1
                                    )
                                } else it
                            }
                        }
                    }
                    is WsEvent.ChatDeleted -> {
                        if (event.project == null) {
                            _uiState.update {
                                if (event.chatName in it.chats) {
                                    it.copy(
                                        chats = it.chats - event.chatName,
                                        total = it.total - 1
                                    )
                                } else it
                            }
                        }
                    }
                    else -> { /* Ignore chat-level events here */ }
                }
            }
        }
    }

    private suspend fun loadChats() {
        val username = _uiState.value.username
        if (username.isBlank()) {
            _uiState.update { it.copy(isLoading = false, isRefreshing = false) }
            return
        }
        try {
            val response = chatRepo.getChats(username, pageSize, 0)
            if (response.isSuccessful) {
                val body = response.body()!!
                _uiState.update {
                    it.copy(
                        chats = body.chats,
                        projects = body.projects,
                        total = body.total,
                        hasMore = body.hasMore,
                        error = null
                    )
                }
            } else {
                _uiState.update { it.copy(error = "Failed to load chats") }
            }
        } catch (e: Exception) {
            _uiState.update { it.copy(error = e.message ?: "Network error") }
        } finally {
            _uiState.update { it.copy(isLoading = false, isRefreshing = false) }
        }
    }

    fun refresh() {
        viewModelScope.launch {
            val state = _uiState.value
            if (state.chats.isEmpty() && state.projects.isEmpty()) {
                _uiState.update { it.copy(isLoading = true, error = null) }
            } else {
                _uiState.update { it.copy(isRefreshing = true) }
            }
            loadChats()
        }
    }

    fun loadMore() {
        if (_uiState.value.isLoadingMore || !_uiState.value.hasMore) return
        viewModelScope.launch {
            _uiState.update { it.copy(isLoadingMore = true) }
            val username = _uiState.value.username
            val offset = _uiState.value.chats.size
            try {
                val response = chatRepo.getChats(username, pageSize, offset)
                if (response.isSuccessful) {
                    val body = response.body()!!
                    _uiState.update {
                        it.copy(
                            chats = it.chats + body.chats,
                            total = body.total,
                            hasMore = body.hasMore
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

    fun createChat(name: String) {
        val trimmed = name.trim()
        if (trimmed.isBlank()) {
            _uiState.update { it.copy(dialogError = "Chat name cannot be empty") }
            return
        }
        viewModelScope.launch {
            _uiState.update { it.copy(isDialogLoading = true, dialogError = null) }
            try {
                val response = chatRepo.createChat(_uiState.value.username, trimmed)
                if (response.isSuccessful) {
                    _uiState.update { it.copy(showCreateDialog = false, isDialogLoading = false, dialogError = null) }
                    loadChats()
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Failed to create chat"
                    _uiState.update { it.copy(dialogError = errorBody, isDialogLoading = false) }
                }
            } catch (e: Exception) {
                _uiState.update { it.copy(dialogError = e.message ?: "Network error", isDialogLoading = false) }
            }
        }
    }

    fun deleteChat(chatName: String) {
        viewModelScope.launch {
            _uiState.update { it.copy(isDialogLoading = true, dialogError = null) }
            try {
                val response = chatRepo.deleteChat(_uiState.value.username, chatName)
                if (response.isSuccessful) {
                    _uiState.update {
                        if (chatName in it.chats) {
                            it.copy(
                                chats = it.chats - chatName,
                                total = it.total - 1,
                                showDeleteDialog = null,
                                isDialogLoading = false,
                                dialogError = null
                            )
                        } else {
                            it.copy(
                                showDeleteDialog = null,
                                isDialogLoading = false,
                                dialogError = null
                            )
                        }
                    }
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Failed to delete chat"
                    _uiState.update { it.copy(dialogError = errorBody, isDialogLoading = false) }
                }
            } catch (e: Exception) {
                _uiState.update { it.copy(dialogError = e.message ?: "Network error", isDialogLoading = false) }
            }
        }
    }

    fun renameChat(oldName: String, newName: String) {
        val trimmed = newName.trim()
        if (trimmed.isBlank()) {
            _uiState.update { it.copy(dialogError = "Chat name cannot be empty") }
            return
        }
        if (trimmed == oldName) {
            _uiState.update { it.copy(showRenameDialog = null, dialogError = null) }
            return
        }
        viewModelScope.launch {
            _uiState.update { it.copy(isDialogLoading = true, dialogError = null) }
            try {
                val response = chatRepo.renameChat(_uiState.value.username, oldName, trimmed)
                if (response.isSuccessful) {
                    _uiState.update {
                        it.copy(
                            chats = it.chats.map { name -> if (name == oldName) trimmed else name },
                            showRenameDialog = null,
                            isDialogLoading = false,
                            dialogError = null
                        )
                    }
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Failed to rename chat"
                    _uiState.update { it.copy(dialogError = errorBody, isDialogLoading = false) }
                }
            } catch (e: Exception) {
                _uiState.update { it.copy(dialogError = e.message ?: "Network error", isDialogLoading = false) }
            }
        }
    }

    fun logout() {
        viewModelScope.launch {
            webSocketManager.disconnectChat()
            webSocketManager.disconnectUser()
            authRepo.logout()
            _navEvents.send(ChatListNavEvent.NavigateToLogin)
        }
    }

    fun showCreateDialog() {
        _uiState.update { it.copy(showCreateDialog = true, dialogError = null) }
    }

    fun dismissCreateDialog() {
        _uiState.update { it.copy(showCreateDialog = false, dialogError = null, isDialogLoading = false) }
    }

    fun showDeleteDialog(chatName: String) {
        _uiState.update { it.copy(showDeleteDialog = chatName, dialogError = null) }
    }

    fun dismissDeleteDialog() {
        _uiState.update { it.copy(showDeleteDialog = null, dialogError = null, isDialogLoading = false) }
    }

    fun showRenameDialog(chatName: String) {
        _uiState.update { it.copy(showRenameDialog = chatName, dialogError = null) }
    }

    fun dismissRenameDialog() {
        _uiState.update { it.copy(showRenameDialog = null, dialogError = null, isDialogLoading = false) }
    }

    fun navigateToChat(chatName: String, project: String? = null) {
        viewModelScope.launch {
            _navEvents.send(ChatListNavEvent.NavigateToChat(chatName, project))
        }
    }

    fun navigateToProject(projectName: String) {
        viewModelScope.launch {
            _navEvents.send(ChatListNavEvent.NavigateToProject(projectName))
        }
    }

    fun navigateToSettings() {
        viewModelScope.launch {
            _navEvents.send(ChatListNavEvent.NavigateToSettings)
        }
    }
}
