package com.chorusai.app.ui.viewmodel

import app.cash.turbine.test
import com.chorusai.app.data.AuthRepository
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.ChatListResponse
import com.chorusai.app.model.WsEvent
import com.chorusai.app.network.WebSocketManager
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.every
import io.mockk.mockk
import kotlinx.coroutines.flow.MutableSharedFlow
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
class ChatListViewModelTest {

    private val testDispatcher = StandardTestDispatcher()
    private lateinit var chatRepo: ChatRepository
    private lateinit var authRepo: AuthRepository
    private lateinit var prefs: UserPreferences
    private lateinit var webSocketManager: WebSocketManager

    @Before
    fun setup() {
        Dispatchers.setMain(testDispatcher)
        chatRepo = mockk(relaxed = true)
        authRepo = mockk(relaxed = true)
        prefs = mockk(relaxed = true)
        webSocketManager = mockk(relaxed = true)
        every { webSocketManager.userEvents } returns MutableSharedFlow<WsEvent>()
        coEvery { prefs.username } returns flowOf("testuser")
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel(): ChatListViewModel {
        return ChatListViewModel(chatRepo, authRepo, prefs, webSocketManager)
    }

    // ---- Init / loadChats ----

    @Test
    fun `init loads username and chats`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(
                chats = listOf("chat1", "chat2"),
                projects = listOf("proj1"),
                total = 2,
                hasMore = false
            )
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals("testuser", state.username)
        assertEquals(listOf("chat1", "chat2"), state.chats)
        assertEquals(listOf("proj1"), state.projects)
        assertEquals(2, state.total)
        assertFalse(state.hasMore)
        assertFalse(state.isLoading)
        assertNull(state.error)
    }

    @Test
    fun `init handles network error`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } throws
                java.io.IOException("Connection refused")

        val vm = createViewModel()
        advanceUntilIdle()

        assertEquals("Connection refused", vm.uiState.value.error)
        assertFalse(vm.uiState.value.isLoading)
    }

    @Test
    fun `init handles API error`() = runTest {
        val errorBody = "Server error".toResponseBody("text/plain".toMediaType())
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.error(500, errorBody)

        val vm = createViewModel()
        advanceUntilIdle()

        assertEquals("Failed to load chats", vm.uiState.value.error)
    }

    @Test
    fun `init with blank username does not call API`() = runTest {
        coEvery { prefs.username } returns flowOf("")

        val vm = createViewModel()
        advanceUntilIdle()

        coVerify(exactly = 0) { chatRepo.getChats(any(), any(), any()) }
        assertFalse(vm.uiState.value.isLoading)
    }

    // ---- refresh ----

    @Test
    fun `refresh reloads chats`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1"), projects = emptyList(), total = 1, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        // Change mock for refresh
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1", "chat2"), projects = emptyList(), total = 2, hasMore = false)
        )

        vm.refresh()
        advanceUntilIdle()

        assertEquals(listOf("chat1", "chat2"), vm.uiState.value.chats)
        assertFalse(vm.uiState.value.isRefreshing)
    }

    // ---- loadMore ----

    @Test
    fun `loadMore appends next page`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1", "chat2"), projects = emptyList(), total = 4, hasMore = true)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        coEvery { chatRepo.getChats("testuser", 20, 2) } returns Response.success(
            ChatListResponse(chats = listOf("chat3", "chat4"), projects = emptyList(), total = 4, hasMore = false)
        )

        vm.loadMore()
        advanceUntilIdle()

        assertEquals(listOf("chat1", "chat2", "chat3", "chat4"), vm.uiState.value.chats)
        assertFalse(vm.uiState.value.hasMore)
        assertFalse(vm.uiState.value.isLoadingMore)
    }

    @Test
    fun `loadMore does nothing when hasMore is false`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1"), projects = emptyList(), total = 1, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.loadMore()
        advanceUntilIdle()

        // Should only have been called once (the init call)
        coVerify(exactly = 1) { chatRepo.getChats(any(), any(), any()) }
    }

    // ---- createChat ----

    @Test
    fun `createChat success reloads list`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        coEvery { chatRepo.createChat("testuser", "new chat") } returns
                Response.success(mapOf("status" to "created"))
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("new chat"), projects = emptyList(), total = 1, hasMore = false)
        )

        vm.showCreateDialog()
        vm.createChat("new chat")
        advanceUntilIdle()

        assertFalse(vm.uiState.value.showCreateDialog)
        assertEquals(listOf("new chat"), vm.uiState.value.chats)
    }

    @Test
    fun `createChat with blank name shows error`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.createChat("   ")
        advanceUntilIdle()

        assertEquals("Chat name cannot be empty", vm.uiState.value.dialogError)
    }

    @Test
    fun `createChat API error shows dialog error`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val errorBody = "Chat already exists".toResponseBody("text/plain".toMediaType())
        coEvery { chatRepo.createChat("testuser", "existing") } returns Response.error(409, errorBody)

        vm.createChat("existing")
        advanceUntilIdle()

        assertTrue(vm.uiState.value.dialogError != null)
        assertFalse(vm.uiState.value.isDialogLoading)
    }

    // ---- deleteChat ----

    @Test
    fun `deleteChat success removes from list`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1", "chat2"), projects = emptyList(), total = 2, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        coEvery { chatRepo.deleteChat("testuser", "chat1") } returns
                Response.success(mapOf("status" to "deleted"))

        vm.deleteChat("chat1")
        advanceUntilIdle()

        assertEquals(listOf("chat2"), vm.uiState.value.chats)
        assertEquals(1, vm.uiState.value.total)
        assertNull(vm.uiState.value.showDeleteDialog)
    }

    @Test
    fun `deleteChat API error shows dialog error`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1"), projects = emptyList(), total = 1, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        val errorBody = "Not found".toResponseBody("text/plain".toMediaType())
        coEvery { chatRepo.deleteChat("testuser", "chat1") } returns Response.error(404, errorBody)

        vm.deleteChat("chat1")
        advanceUntilIdle()

        assertTrue(vm.uiState.value.dialogError != null)
        assertEquals(listOf("chat1"), vm.uiState.value.chats) // not removed
    }

    // ---- renameChat ----

    @Test
    fun `renameChat success updates list`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("old name"), projects = emptyList(), total = 1, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        coEvery { chatRepo.renameChat("testuser", "old name", "new name") } returns
                Response.success(mapOf("status" to "renamed"))

        vm.renameChat("old name", "new name")
        advanceUntilIdle()

        assertEquals(listOf("new name"), vm.uiState.value.chats)
        assertNull(vm.uiState.value.showRenameDialog)
    }

    @Test
    fun `renameChat with blank name shows error`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1"), projects = emptyList(), total = 1, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.renameChat("chat1", "  ")
        advanceUntilIdle()

        assertEquals("Chat name cannot be empty", vm.uiState.value.dialogError)
    }

    @Test
    fun `renameChat with same name dismisses dialog`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = listOf("chat1"), projects = emptyList(), total = 1, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.showRenameDialog("chat1")
        vm.renameChat("chat1", "chat1")
        advanceUntilIdle()

        assertNull(vm.uiState.value.showRenameDialog)
        coVerify(exactly = 0) { chatRepo.renameChat(any(), any(), any()) }
    }

    // ---- logout ----

    @Test
    fun `logout calls authRepo and navigates to login`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.navEvents.test {
            vm.logout()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatListNavEvent.NavigateToLogin)
            cancelAndIgnoreRemainingEvents()
        }

        coVerify { authRepo.logout() }
    }

    // ---- navigation helpers ----

    @Test
    fun `navigateToChat emits NavigateToChat event`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.navEvents.test {
            vm.navigateToChat("mychat", "myproject")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatListNavEvent.NavigateToChat)
            val nav = event as ChatListNavEvent.NavigateToChat
            assertEquals("mychat", nav.chatName)
            assertEquals("myproject", nav.project)
            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `navigateToProject emits NavigateToProject event`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.navEvents.test {
            vm.navigateToProject("myproject")
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatListNavEvent.NavigateToProject)
            assertEquals("myproject", (event as ChatListNavEvent.NavigateToProject).projectName)
            cancelAndIgnoreRemainingEvents()
        }
    }

    @Test
    fun `navigateToSettings emits NavigateToSettings event`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.navEvents.test {
            vm.navigateToSettings()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatListNavEvent.NavigateToSettings)
            cancelAndIgnoreRemainingEvents()
        }
    }

    // ---- dialog state helpers ----

    @Test
    fun `showCreateDialog and dismissCreateDialog toggle state`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.showCreateDialog()
        assertTrue(vm.uiState.value.showCreateDialog)

        vm.dismissCreateDialog()
        assertFalse(vm.uiState.value.showCreateDialog)
    }

    @Test
    fun `showDeleteDialog and dismissDeleteDialog toggle state`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.showDeleteDialog("chat1")
        assertEquals("chat1", vm.uiState.value.showDeleteDialog)

        vm.dismissDeleteDialog()
        assertNull(vm.uiState.value.showDeleteDialog)
    }

    @Test
    fun `showRenameDialog and dismissRenameDialog toggle state`() = runTest {
        coEvery { chatRepo.getChats("testuser", 20, 0) } returns Response.success(
            ChatListResponse(chats = emptyList(), projects = emptyList(), total = 0, hasMore = false)
        )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.showRenameDialog("chat1")
        assertEquals("chat1", vm.uiState.value.showRenameDialog)

        vm.dismissRenameDialog()
        assertNull(vm.uiState.value.showRenameDialog)
    }
}
