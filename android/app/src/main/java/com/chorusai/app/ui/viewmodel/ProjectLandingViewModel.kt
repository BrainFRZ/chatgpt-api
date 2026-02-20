package com.chorusai.app.ui.viewmodel

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.ProjectRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.ChatSummary
import com.chorusai.app.model.ModelInfo
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import javax.inject.Inject

data class ProjectLandingUiState(
    val isLoading: Boolean = true,
    val isRefreshing: Boolean = false,
    val error: String? = null,
    val username: String = "",
    val projectName: String = "",
    val chats: List<ChatSummary> = emptyList(),
    val total: Int = 0,
    val hasMore: Boolean = false,
    val isLoadingMore: Boolean = false,
    val searchQuery: String = "",
    val currentModel: String? = null,
    val models: List<ModelInfo> = emptyList(),
    val showCreateDialog: Boolean = false,
    val dialogError: String? = null,
    val isDialogLoading: Boolean = false
)

sealed class ProjectLandingNavEvent {
    data class NavigateToChat(val chatName: String, val project: String) : ProjectLandingNavEvent()
    data object NavigateBack : ProjectLandingNavEvent()
}

@HiltViewModel
class ProjectLandingViewModel @Inject constructor(
    private val projectRepo: ProjectRepository,
    private val chatRepo: ChatRepository,
    private val prefs: UserPreferences,
    savedStateHandle: SavedStateHandle
) : ViewModel() {

    private val _uiState = MutableStateFlow(ProjectLandingUiState())
    val uiState: StateFlow<ProjectLandingUiState> = _uiState.asStateFlow()

    private val _navEvents = Channel<ProjectLandingNavEvent>(Channel.BUFFERED)
    val navEvents = _navEvents.receiveAsFlow()

    private val pageSize = 50

    init {
        val projectName = savedStateHandle.get<String>("projectName") ?: ""
        _uiState.update { it.copy(projectName = projectName) }

        viewModelScope.launch {
            val username = prefs.username.first() ?: ""
            _uiState.update { it.copy(username = username) }
            loadProjectData()
        }
    }

    private suspend fun loadProjectData() {
        val state = _uiState.value
        if (state.username.isBlank() || state.projectName.isBlank()) {
            _uiState.update { it.copy(isLoading = false, isRefreshing = false) }
            return
        }
        try {
            val (metadataResponse, chatsResponse, modelsResponse) = coroutineScope {
                val metadataDeferred = async {
                    projectRepo.getProjectMetadata(state.username, state.projectName)
                }
                val chatsDeferred = async {
                    projectRepo.getProjectChatsDetailed(state.username, state.projectName, pageSize, 0)
                }
                val modelsDeferred = async {
                    chatRepo.getModels()
                }
                Triple(metadataDeferred.await(), chatsDeferred.await(), modelsDeferred.await())
            }

            _uiState.update {
                it.copy(
                    currentModel = if (metadataResponse.isSuccessful) metadataResponse.body()?.model else it.currentModel,
                    chats = if (chatsResponse.isSuccessful) chatsResponse.body()?.chats ?: emptyList() else it.chats,
                    total = if (chatsResponse.isSuccessful) chatsResponse.body()?.total ?: 0 else it.total,
                    hasMore = if (chatsResponse.isSuccessful) chatsResponse.body()?.hasMore ?: false else it.hasMore,
                    models = if (modelsResponse.isSuccessful) modelsResponse.body() ?: emptyList() else it.models,
                    error = null
                )
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
            if (state.chats.isEmpty()) {
                _uiState.update { it.copy(isLoading = true, error = null) }
            } else {
                _uiState.update { it.copy(isRefreshing = true) }
            }
            loadProjectData()
        }
    }

    fun loadMore() {
        if (_uiState.value.isLoadingMore || !_uiState.value.hasMore) return
        viewModelScope.launch {
            _uiState.update { it.copy(isLoadingMore = true) }
            val state = _uiState.value
            try {
                val response = projectRepo.getProjectChatsDetailed(
                    state.username, state.projectName, pageSize, state.chats.size
                )
                if (response.isSuccessful) {
                    val body = response.body() ?: return@launch
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

    fun updateSearchQuery(query: String) {
        _uiState.update { it.copy(searchQuery = query) }
    }

    fun setProjectModel(modelId: String) {
        val state = _uiState.value
        val previousModel = state.currentModel
        _uiState.update { it.copy(currentModel = modelId) }
        viewModelScope.launch {
            try {
                val response = projectRepo.setProjectModel(
                    state.username, state.projectName, modelId
                )
                if (!response.isSuccessful) {
                    _uiState.update {
                        if (it.currentModel == modelId) it.copy(currentModel = previousModel) else it
                    }
                }
            } catch (_: Exception) {
                _uiState.update {
                    if (it.currentModel == modelId) it.copy(currentModel = previousModel) else it
                }
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
                val state = _uiState.value
                val response = chatRepo.createChat(
                    state.username, trimmed, state.projectName, state.currentModel
                )
                if (response.isSuccessful) {
                    _uiState.update {
                        it.copy(showCreateDialog = false, isDialogLoading = false, dialogError = null)
                    }
                    _navEvents.send(
                        ProjectLandingNavEvent.NavigateToChat(trimmed, state.projectName)
                    )
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Failed to create chat"
                    _uiState.update { it.copy(dialogError = errorBody, isDialogLoading = false) }
                }
            } catch (e: Exception) {
                _uiState.update {
                    it.copy(dialogError = e.message ?: "Network error", isDialogLoading = false)
                }
            }
        }
    }

    fun navigateToChat(chatName: String) {
        viewModelScope.launch {
            _navEvents.send(
                ProjectLandingNavEvent.NavigateToChat(chatName, _uiState.value.projectName)
            )
        }
    }

    fun navigateBack() {
        viewModelScope.launch {
            _navEvents.send(ProjectLandingNavEvent.NavigateBack)
        }
    }

    fun showCreateDialog() {
        _uiState.update { it.copy(showCreateDialog = true, dialogError = null) }
    }

    fun dismissCreateDialog() {
        _uiState.update { it.copy(showCreateDialog = false, dialogError = null, isDialogLoading = false) }
    }
}
