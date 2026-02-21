package com.chorusai.app.ui.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.chorusai.app.data.AuthRepository
import com.chorusai.app.data.UserPreferences
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

data class AuthUiState(
    val isLoading: Boolean = false,
    val error: String? = null,
    val isCheckingAutoLogin: Boolean = true,
    val username: String = "",
    val hasOpenai: Boolean = false,
    val hasAnthropic: Boolean = false
)

sealed class NavigationEvent {
    data object NavigateToChatList : NavigationEvent()
    data class NavigateToChat(val chatName: String, val project: String?) : NavigationEvent()
    data class NavigateToApiKeySetup(val username: String, val isNewUser: Boolean) : NavigationEvent()
    data object NavigateToLogin : NavigationEvent()
}

@HiltViewModel
class AuthViewModel @Inject constructor(
    private val authRepo: AuthRepository,
    private val prefs: UserPreferences
) : ViewModel() {

    private val _uiState = MutableStateFlow(AuthUiState())
    val uiState: StateFlow<AuthUiState> = _uiState.asStateFlow()

    private val _navigationEvents = Channel<NavigationEvent>(Channel.BUFFERED)
    val navigationEvents = _navigationEvents.receiveAsFlow()

    fun checkAutoLogin() {
        viewModelScope.launch {
            _uiState.update { it.copy(isCheckingAutoLogin = true) }
            try {
                val isLoggedIn = prefs.isLoggedIn.first()
                val username = prefs.username.first()
                if (isLoggedIn && !username.isNullOrBlank()) {
                    val lastChat = prefs.getLastChat()
                    if (lastChat != null) {
                        _navigationEvents.send(
                            NavigationEvent.NavigateToChat(lastChat.first, lastChat.second)
                        )
                    } else {
                        _navigationEvents.send(NavigationEvent.NavigateToChatList)
                    }
                }
            } catch (_: Exception) {
                // Auto-login failed, show login screen
            } finally {
                _uiState.update { it.copy(isCheckingAutoLogin = false) }
            }
        }
    }

    fun updateUsername(value: String) {
        val filtered = value.filter { it.isLetterOrDigit() }
        _uiState.update { it.copy(username = filtered, error = null) }
    }

    fun login() {
        val username = _uiState.value.username.trim()
        if (username.length < 2) {
            _uiState.update { it.copy(error = "Username must be at least 2 characters") }
            return
        }

        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, error = null) }
            try {
                val response = authRepo.login(username)
                if (response.isSuccessful) {
                    val body = response.body() ?: run {
                        _uiState.update { it.copy(error = "Invalid server response") }
                        return@launch
                    }
                    val serverUsername = body.username
                    authRepo.saveLogin(serverUsername)
                    if (body.hasApiKey) {
                        _navigationEvents.send(NavigationEvent.NavigateToChatList)
                    } else {
                        _navigationEvents.send(
                            NavigationEvent.NavigateToApiKeySetup(
                                username = serverUsername,
                                isNewUser = body.isNewUser
                            )
                        )
                    }
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Login failed"
                    _uiState.update { it.copy(error = errorBody) }
                }
            } catch (e: Exception) {
                _uiState.update {
                    it.copy(error = e.message ?: "Network error. Check your connection.")
                }
            } finally {
                _uiState.update { it.copy(isLoading = false) }
            }
        }
    }

    fun loadApiKeyStatus(username: String) {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true) }
            try {
                val response = authRepo.getApiKeys(username)
                if (response.isSuccessful) {
                    val body = response.body() ?: return@launch
                    _uiState.update {
                        it.copy(hasOpenai = body.hasOpenai, hasAnthropic = body.hasAnthropic)
                    }
                }
            } catch (_: Exception) {
                // Non-critical, just won't show status indicators
            } finally {
                _uiState.update { it.copy(isLoading = false) }
            }
        }
    }

    fun saveApiKeys(username: String, openaiKey: String, anthropicKey: String) {
        val trimmedOpenai = openaiKey.trim().ifBlank { null }
        val trimmedAnthropic = anthropicKey.trim().ifBlank { null }

        if (trimmedOpenai == null && trimmedAnthropic == null &&
            !_uiState.value.hasOpenai && !_uiState.value.hasAnthropic
        ) {
            _uiState.update { it.copy(error = "Enter at least one API key") }
            return
        }

        viewModelScope.launch {
            _uiState.update { it.copy(isLoading = true, error = null) }
            try {
                val response = authRepo.setApiKeys(username, trimmedOpenai, trimmedAnthropic)
                if (response.isSuccessful) {
                    _navigationEvents.send(NavigationEvent.NavigateToChatList)
                } else {
                    val errorBody = response.errorBody()?.string() ?: "Failed to save API keys"
                    _uiState.update { it.copy(error = errorBody) }
                }
            } catch (e: Exception) {
                _uiState.update {
                    it.copy(error = e.message ?: "Network error. Check your connection.")
                }
            } finally {
                _uiState.update { it.copy(isLoading = false) }
            }
        }
    }

    fun logout() {
        viewModelScope.launch {
            authRepo.logout()
            _uiState.update { AuthUiState(isCheckingAutoLogin = false) }
            _navigationEvents.send(NavigationEvent.NavigateToLogin)
        }
    }
}
