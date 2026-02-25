package com.chorusai.app.ui.viewmodel

import app.cash.turbine.test
import com.chorusai.app.data.AuthRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.ApiKeysResponse
import com.chorusai.app.model.LoginResponse
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.mockk
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.Response

@OptIn(ExperimentalCoroutinesApi::class)
class AuthViewModelTest {

    private val testDispatcher = StandardTestDispatcher()
    private lateinit var authRepo: AuthRepository
    private lateinit var prefs: UserPreferences
    private lateinit var viewModel: AuthViewModel

    @Before
    fun setup() {
        Dispatchers.setMain(testDispatcher)
        authRepo = mockk(relaxed = true)
        prefs = mockk(relaxed = true)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel(): AuthViewModel {
        return AuthViewModel(authRepo, prefs)
    }

    // ---- Initial State ----

    @Test
    fun `initial state has isCheckingAutoLogin true`() {
        viewModel = createViewModel()
        val state = viewModel.uiState.value
        assertTrue(state.isCheckingAutoLogin)
        assertFalse(state.isLoading)
        assertNull(state.error)
        assertEquals("", state.username)
        assertFalse(state.hasOpenai)
        assertFalse(state.hasAnthropic)
    }

    // ---- checkAutoLogin ----

    @Test
    fun `checkAutoLogin navigates to ChatList when logged in`() = runTest {
        coEvery { prefs.isLoggedIn } returns flowOf(true)
        coEvery { prefs.username } returns flowOf("testuser")
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.checkAutoLogin()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToChatList)
            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `checkAutoLogin sets isCheckingAutoLogin false when not logged in`() = runTest {
        coEvery { prefs.isLoggedIn } returns flowOf(false)
        coEvery { prefs.username } returns flowOf(null)
        viewModel = createViewModel()

        viewModel.checkAutoLogin()
        advanceUntilIdle()

        assertFalse(viewModel.uiState.value.isCheckingAutoLogin)
    }

    @Test
    fun `checkAutoLogin does not navigate when logged in but username blank`() = runTest {
        coEvery { prefs.isLoggedIn } returns flowOf(true)
        coEvery { prefs.username } returns flowOf("")
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.checkAutoLogin()
            advanceUntilIdle()

            expectNoEvents()
            cancelAndIgnoreRemainingEvents()
        }

        assertFalse(viewModel.uiState.value.isCheckingAutoLogin)
    }

    @Test
    fun `checkAutoLogin handles exception gracefully`() = runTest {
        coEvery { prefs.isLoggedIn } returns flowOf(true)
        coEvery { prefs.username } throws RuntimeException("DataStore error")
        viewModel = createViewModel()

        viewModel.checkAutoLogin()
        advanceUntilIdle()

        assertFalse(viewModel.uiState.value.isCheckingAutoLogin)
    }

    // ---- updateUsername ----

    @Test
    fun `updateUsername filters non-alphanumeric characters`() {
        viewModel = createViewModel()

        viewModel.updateUsername("test@user!123")
        assertEquals("testuser123", viewModel.uiState.value.username)
    }

    @Test
    fun `updateUsername allows pure alphanumeric`() {
        viewModel = createViewModel()

        viewModel.updateUsername("Alice42")
        assertEquals("Alice42", viewModel.uiState.value.username)
    }

    @Test
    fun `updateUsername clears error`() = runTest {
        viewModel = createViewModel()

        // Trigger an error first via short username login
        viewModel.updateUsername("a")
        viewModel.login()
        advanceUntilIdle()

        assertTrue(viewModel.uiState.value.error != null)

        viewModel.updateUsername("ab")
        assertNull(viewModel.uiState.value.error)
    }

    @Test
    fun `updateUsername strips spaces`() {
        viewModel = createViewModel()

        viewModel.updateUsername("hello world")
        assertEquals("helloworld", viewModel.uiState.value.username)
    }

    // ---- login ----

    @Test
    fun `login with empty username shows error`() = runTest {
        viewModel = createViewModel()

        viewModel.login()
        advanceUntilIdle()

        assertEquals("Username must be at least 2 characters", viewModel.uiState.value.error)
    }

    @Test
    fun `login with single char username shows error`() = runTest {
        viewModel = createViewModel()

        viewModel.updateUsername("a")
        viewModel.login()
        advanceUntilIdle()

        assertEquals("Username must be at least 2 characters", viewModel.uiState.value.error)
    }

    @Test
    fun `login success with hasApiKey navigates to ChatList`() = runTest {
        coEvery { authRepo.login("testuser") } returns Response.success(
            LoginResponse(username = "testuser", hasApiKey = true, isNewUser = false)
        )
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.updateUsername("testuser")
            viewModel.login()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToChatList)
            cancelAndIgnoreRemainingEvents()
        }

        coVerify { authRepo.saveLogin("testuser") }
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `login success without apiKey navigates to ApiKeySetup`() = runTest {
        coEvery { authRepo.login("newuser") } returns Response.success(
            LoginResponse(username = "newuser", hasApiKey = false, isNewUser = true)
        )
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.updateUsername("newuser")
            viewModel.login()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToApiKeySetup)
            val setup = event as NavigationEvent.NavigateToApiKeySetup
            assertEquals("newuser", setup.username)
            assertTrue(setup.isNewUser)
            cancelAndIgnoreRemainingEvents()
        }

        coVerify { authRepo.saveLogin("newuser") }
    }

    @Test
    fun `login success returning user without apiKey sets isNewUser false`() = runTest {
        coEvery { authRepo.login("olduser") } returns Response.success(
            LoginResponse(username = "olduser", hasApiKey = false, isNewUser = false)
        )
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.updateUsername("olduser")
            viewModel.login()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToApiKeySetup)
            assertFalse((event as NavigationEvent.NavigateToApiKeySetup).isNewUser)
            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `login API error shows error message`() = runTest {
        val errorBody = """{"detail":"User not found"}""".toResponseBody("application/json".toMediaType())
        coEvery { authRepo.login("baduser") } returns Response.error(404, errorBody)
        viewModel = createViewModel()

        viewModel.updateUsername("baduser")
        viewModel.login()
        advanceUntilIdle()

        assertTrue(viewModel.uiState.value.error != null)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `login network error shows error message`() = runTest {
        coEvery { authRepo.login("testuser") } throws java.io.IOException("Connection refused")
        viewModel = createViewModel()

        viewModel.updateUsername("testuser")
        viewModel.login()
        advanceUntilIdle()

        assertEquals("Connection refused", viewModel.uiState.value.error)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `login sets isLoading during request`() = runTest {
        coEvery { authRepo.login("testuser") } returns Response.success(
            LoginResponse(username = "testuser", hasApiKey = true, isNewUser = false)
        )
        viewModel = createViewModel()

        viewModel.updateUsername("testuser")
        viewModel.login()
        // Before advancing, isLoading should not yet be set since coroutine hasn't started
        advanceUntilIdle()

        // After completion, isLoading should be false
        assertFalse(viewModel.uiState.value.isLoading)
    }

    // ---- loadApiKeyStatus ----

    @Test
    fun `loadApiKeyStatus updates hasOpenai and hasAnthropic`() = runTest {
        coEvery { authRepo.getApiKeys("testuser") } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = false)
        )
        viewModel = createViewModel()

        viewModel.loadApiKeyStatus("testuser")
        advanceUntilIdle()

        assertTrue(viewModel.uiState.value.hasOpenai)
        assertFalse(viewModel.uiState.value.hasAnthropic)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `loadApiKeyStatus both keys configured`() = runTest {
        coEvery { authRepo.getApiKeys("testuser") } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = true)
        )
        viewModel = createViewModel()

        viewModel.loadApiKeyStatus("testuser")
        advanceUntilIdle()

        assertTrue(viewModel.uiState.value.hasOpenai)
        assertTrue(viewModel.uiState.value.hasAnthropic)
    }

    @Test
    fun `loadApiKeyStatus handles error gracefully`() = runTest {
        coEvery { authRepo.getApiKeys("testuser") } throws RuntimeException("Network error")
        viewModel = createViewModel()

        viewModel.loadApiKeyStatus("testuser")
        advanceUntilIdle()

        // Should not crash, just leave status as false
        assertFalse(viewModel.uiState.value.hasOpenai)
        assertFalse(viewModel.uiState.value.hasAnthropic)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `loadApiKeyStatus handles API error response`() = runTest {
        val errorBody = "error".toResponseBody("text/plain".toMediaType())
        coEvery { authRepo.getApiKeys("testuser") } returns Response.error(500, errorBody)
        viewModel = createViewModel()

        viewModel.loadApiKeyStatus("testuser")
        advanceUntilIdle()

        assertFalse(viewModel.uiState.value.hasOpenai)
        assertFalse(viewModel.uiState.value.hasAnthropic)
    }

    // ---- saveApiKeys ----

    @Test
    fun `saveApiKeys with both empty and no existing keys shows error`() = runTest {
        viewModel = createViewModel()

        viewModel.saveApiKeys("testuser", "", "")
        advanceUntilIdle()

        assertEquals("Enter at least one API key", viewModel.uiState.value.error)
    }

    @Test
    fun `saveApiKeys with blank spaces and no existing keys shows error`() = runTest {
        viewModel = createViewModel()

        viewModel.saveApiKeys("testuser", "   ", "  ")
        advanceUntilIdle()

        assertEquals("Enter at least one API key", viewModel.uiState.value.error)
    }

    @Test
    fun `saveApiKeys success navigates to ChatList`() = runTest {
        coEvery { authRepo.setApiKeys("testuser", "sk-abc", null) } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = false)
        )
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.saveApiKeys("testuser", "sk-abc", "")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToChatList)
            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `saveApiKeys with both keys sends both`() = runTest {
        coEvery { authRepo.setApiKeys("testuser", "sk-abc", "sk-ant") } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = true)
        )
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.saveApiKeys("testuser", "sk-abc", "sk-ant")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToChatList)
            cancelAndIgnoreRemainingEvents()
        }

        coVerify { authRepo.setApiKeys("testuser", "sk-abc", "sk-ant") }
    }

    @Test
    fun `saveApiKeys trims whitespace from keys`() = runTest {
        coEvery { authRepo.setApiKeys("testuser", "sk-abc", null) } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = false)
        )
        viewModel = createViewModel()

        viewModel.saveApiKeys("testuser", "  sk-abc  ", "   ")
        advanceUntilIdle()

        coVerify { authRepo.setApiKeys("testuser", "sk-abc", null) }
    }

    @Test
    fun `saveApiKeys with empty keys but existing keys allows save`() = runTest {
        // First load existing key status
        coEvery { authRepo.getApiKeys("testuser") } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = false)
        )
        coEvery { authRepo.setApiKeys("testuser", null, null) } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = false)
        )
        viewModel = createViewModel()

        viewModel.loadApiKeyStatus("testuser")
        advanceUntilIdle()

        viewModel.navigationEvents.test {
            viewModel.saveApiKeys("testuser", "", "")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToChatList)
            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `saveApiKeys API error shows error`() = runTest {
        val errorBody = "Invalid key".toResponseBody("text/plain".toMediaType())
        coEvery { authRepo.setApiKeys("testuser", "bad-key", null) } returns Response.error(400, errorBody)
        viewModel = createViewModel()

        viewModel.saveApiKeys("testuser", "bad-key", "")
        advanceUntilIdle()

        assertTrue(viewModel.uiState.value.error != null)
        assertFalse(viewModel.uiState.value.isLoading)
    }

    @Test
    fun `saveApiKeys network error shows error`() = runTest {
        coEvery { authRepo.setApiKeys("testuser", "sk-abc", null) } throws
                java.io.IOException("Timeout")
        viewModel = createViewModel()

        viewModel.saveApiKeys("testuser", "sk-abc", "")
        advanceUntilIdle()

        assertEquals("Timeout", viewModel.uiState.value.error)
    }

    // ---- logout ----

    @Test
    fun `logout clears state and navigates to Login`() = runTest {
        viewModel = createViewModel()

        viewModel.navigationEvents.test {
            viewModel.logout()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is NavigationEvent.NavigateToLogin)
            cancelAndIgnoreRemainingEvents()
        }

        coVerify { authRepo.logout() }
        assertFalse(viewModel.uiState.value.isCheckingAutoLogin)
        assertEquals("", viewModel.uiState.value.username)
        assertNull(viewModel.uiState.value.error)
    }

    @Test
    fun `logout resets hasOpenai and hasAnthropic`() = runTest {
        // First set some key status
        coEvery { authRepo.getApiKeys("testuser") } returns Response.success(
            ApiKeysResponse(hasOpenai = true, hasAnthropic = true)
        )
        viewModel = createViewModel()

        viewModel.loadApiKeyStatus("testuser")
        advanceUntilIdle()
        assertTrue(viewModel.uiState.value.hasOpenai)

        viewModel.logout()
        advanceUntilIdle()

        assertFalse(viewModel.uiState.value.hasOpenai)
        assertFalse(viewModel.uiState.value.hasAnthropic)
    }
}
