package com.chorusai.app.ui.viewmodel

import androidx.lifecycle.SavedStateHandle
import app.cash.turbine.test
import com.chorusai.app.data.ChatRepository
import com.chorusai.app.data.UserPreferences
import com.chorusai.app.model.ChatMessage
import com.chorusai.app.model.ChatResponse
import com.chorusai.app.model.ContextLimits
import com.chorusai.app.model.MessageResponse
import com.chorusai.app.model.ModelInfo
import com.chorusai.app.model.ModelPricing
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
class ChatViewModelTest {

    private val testDispatcher = StandardTestDispatcher()
    private lateinit var chatRepo: ChatRepository
    private lateinit var prefs: UserPreferences

    private val testMessages = listOf(
        ChatMessage(id = "1", role = "system", content = "You are a helpful assistant"),
        ChatMessage(id = "2", role = "user", content = "Hello"),
        ChatMessage(
            id = "3", role = "assistant", content = "Hi there!",
            tokens = "50", cost = "0.001", model = "gpt-4"
        )
    )

    @Before
    fun setup() {
        Dispatchers.setMain(testDispatcher)
        chatRepo = mockk(relaxed = true)
        prefs = mockk(relaxed = true)
        coEvery { prefs.username } returns flowOf("testuser")
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun createViewModel(
        chatName: String = "test-chat",
        project: String? = null
    ): ChatViewModel {
        val savedStateHandle = SavedStateHandle(
            buildMap {
                put("chatName", chatName)
                if (project != null) put("project", project)
            }
        )
        return ChatViewModel(chatRepo, prefs, savedStateHandle)
    }

    // ---- Init / loadChat ----

    @Test
    fun `init loads username and messages`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false,
                        currentLeafId = "3",
                        model = "gpt-4"
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals("testuser", state.username)
        assertEquals("test-chat", state.chatName)
        assertEquals(3, state.messages.size)
        assertEquals("1", state.messages[0].id)
        assertEquals("3", state.messages[2].id)
        assertEquals(3, state.totalMessages)
        assertFalse(state.hasMoreMessages)
        assertEquals("3", state.currentLeafId)
        assertEquals("gpt-4", state.model)
        assertFalse(state.isLoading)
        assertNull(state.error)
    }

    @Test
    fun `init handles network error`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } throws
                java.io.IOException("Connection refused")

        val vm = createViewModel()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals("Connection refused", state.error)
        assertFalse(state.isLoading)
        assertTrue(state.messages.isEmpty())
    }

    @Test
    fun `init handles API error`() = runTest {
        val errorBody = "Not found".toResponseBody("text/plain".toMediaType())
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.error(404, errorBody)

        val vm = createViewModel()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals("Failed to load chat", state.error)
        assertFalse(state.isLoading)
        assertTrue(state.messages.isEmpty())
        assertEquals(0, state.totalMessages)
    }

    @Test
    fun `init with project passes project to API`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", "myproject", null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false
                    )
                )

        val vm = createViewModel(project = "myproject")
        advanceUntilIdle()

        assertEquals("myproject", vm.uiState.value.project)
        coVerify { chatRepo.getChat("testuser", "test-chat", "myproject", null, 30, 0) }
    }

    // ---- loadMore ----

    @Test
    fun `loadMore prepends older messages`() = runTest {
        val recentMessages = listOf(
            ChatMessage(id = "3", role = "user", content = "Third"),
            ChatMessage(id = "4", role = "assistant", content = "Fourth")
        )
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = recentMessages,
                        totalMessages = 4,
                        hasMoreMessages = true,
                        currentLeafId = "4"
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        assertTrue(vm.uiState.value.hasMoreMessages)
        assertEquals(2, vm.uiState.value.messages.size)

        val olderMessages = listOf(
            ChatMessage(id = "1", role = "user", content = "First"),
            ChatMessage(id = "2", role = "assistant", content = "Second")
        )
        // loadMore now passes currentLeafId ("4") to the API
        coEvery { chatRepo.getChat("testuser", "test-chat", null, "4", 30, 2) } returns
                Response.success(
                    ChatResponse(
                        messages = olderMessages,
                        totalMessages = 4,
                        hasMoreMessages = false
                    )
                )

        vm.loadMore()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals(4, state.messages.size)
        assertEquals("1", state.messages[0].id)
        assertEquals("2", state.messages[1].id)
        assertEquals("3", state.messages[2].id)
        assertEquals("4", state.messages[3].id)
        assertFalse(state.hasMoreMessages)
        assertFalse(state.isLoadingMore)
    }

    @Test
    fun `loadMore does nothing when hasMoreMessages is false`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.loadMore()
        advanceUntilIdle()

        // Should only have been called once (the init call)
        coVerify(exactly = 1) { chatRepo.getChat(any(), any(), any(), any(), any(), any()) }
    }

    // ---- refresh ----

    @Test
    fun `refresh reloads messages from scratch`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        assertEquals(3, vm.uiState.value.messages.size)

        val updatedMessages = testMessages + ChatMessage(
            id = "4", role = "user", content = "New message"
        )
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = updatedMessages,
                        totalMessages = 4,
                        hasMoreMessages = false
                    )
                )

        vm.refresh()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals(4, state.messages.size)
        assertEquals("4", state.messages[3].id)
        assertFalse(state.isLoading)
        assertNull(state.error)
    }

    // ---- navigateBack ----

    @Test
    fun `navigateBack emits NavigateBack event`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = emptyList(),
                        totalMessages = 0,
                        hasMoreMessages = false
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.navEvents.test {
            vm.navigateBack()
            advanceUntilIdle()

            val event = awaitItem()
            assertTrue(event is ChatNavEvent.NavigateBack)
            cancelAndIgnoreRemainingEvents()
        }
    }

    // ---- sendMessage ----

    @Test
    fun `sendMessage success adds user and assistant messages`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false,
                        currentLeafId = "3",
                        model = "gpt-4"
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        coEvery {
            chatRepo.sendMessage("testuser", "test-chat", "Hello world", null, "3", null, "gpt-4")
        } returns Response.success(
            MessageResponse(
                assistantMessage = "Hi! How can I help?",
                tokens = "100",
                cost = "0.002",
                contextStartIndex = 0,
                userMessageId = "4",
                assistantMessageId = "5",
                currentLeafId = "5",
                totalMessages = 5,
                model = "gpt-4"
            )
        )

        vm.sendMessage("Hello world")
        advanceUntilIdle()

        val state = vm.uiState.value
        assertFalse(state.isSending)
        assertNull(state.sendError)
        // Original 3 + user + assistant = 5
        assertEquals(5, state.messages.size)
        // The user message should have the real ID
        val userMsg = state.messages[3]
        assertEquals("4", userMsg.id)
        assertEquals("user", userMsg.role)
        assertEquals("Hello world", userMsg.content)
        // The assistant message should have the real ID and content
        val assistantMsg = state.messages[4]
        assertEquals("5", assistantMsg.id)
        assertEquals("assistant", assistantMsg.role)
        assertEquals("Hi! How can I help?", assistantMsg.content)
        assertEquals("100", assistantMsg.tokens)
        assertEquals("0.002", assistantMsg.cost)
        // currentLeafId updated
        assertEquals("5", state.currentLeafId)
        assertEquals(5, state.totalMessages)
    }

    @Test
    fun `sendMessage error removes thinking placeholder`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false,
                        currentLeafId = "3",
                        model = "gpt-4"
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        coEvery {
            chatRepo.sendMessage("testuser", "test-chat", "Hello", null, "3", null, "gpt-4")
        } throws java.io.IOException("Network error")

        vm.sendMessage("Hello")
        advanceUntilIdle()

        val state = vm.uiState.value
        assertFalse(state.isSending)
        assertEquals("Network error", state.sendError)
        // Original 3 + user message kept, thinking removed = 4
        assertEquals(4, state.messages.size)
        assertEquals("user", state.messages[3].role)
        assertEquals("Hello", state.messages[3].content)
    }

    @Test
    fun `sendMessage with blank content does nothing`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        vm.sendMessage("   ")
        advanceUntilIdle()

        assertEquals(3, vm.uiState.value.messages.size)
        coVerify(exactly = 0) { chatRepo.sendMessage(any(), any(), any(), any(), any(), any(), any()) }
    }

    // ---- setModel ----

    @Test
    fun `setModel updates model on success`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false,
                        model = "gpt-4"
                    )
                )
        coEvery {
            chatRepo.setChatModel("testuser", "test-chat", null, "gpt-4o")
        } returns Response.success(mapOf("status" to "ok"))

        val vm = createViewModel()
        advanceUntilIdle()

        assertEquals("gpt-4", vm.uiState.value.model)

        vm.setModel("gpt-4o")
        advanceUntilIdle()

        assertEquals("gpt-4o", vm.uiState.value.model)
    }

    @Test
    fun `setModel reverts on failure`() = runTest {
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = testMessages,
                        totalMessages = 3,
                        hasMoreMessages = false,
                        model = "gpt-4"
                    )
                )
        coEvery {
            chatRepo.setChatModel("testuser", "test-chat", null, "gpt-4o")
        } throws java.io.IOException("Network error")

        val vm = createViewModel()
        advanceUntilIdle()

        assertEquals("gpt-4", vm.uiState.value.model)

        vm.setModel("gpt-4o")
        advanceUntilIdle()

        // Should revert to original
        assertEquals("gpt-4", vm.uiState.value.model)
    }

    // ---- loadModels ----

    @Test
    fun `loadModels populates models list`() = runTest {
        val testModels = listOf(
            ModelInfo(
                id = "gpt-4",
                name = "GPT-4",
                pricing = ModelPricing(0.03, 0.01, 0.06, 0.0),
                contextLimits = ContextLimits(8000, 6000)
            ),
            ModelInfo(
                id = "gpt-4o",
                name = "GPT-4o",
                pricing = ModelPricing(0.005, 0.0025, 0.015, 0.0),
                contextLimits = ContextLimits(128000, 100000)
            )
        )

        coEvery { chatRepo.getModels() } returns Response.success(testModels)
        coEvery { chatRepo.getChat("testuser", "test-chat", null, null, 30, 0) } returns
                Response.success(
                    ChatResponse(
                        messages = emptyList(),
                        totalMessages = 0,
                        hasMoreMessages = false
                    )
                )

        val vm = createViewModel()
        advanceUntilIdle()

        val state = vm.uiState.value
        assertEquals(2, state.models.size)
        assertEquals("gpt-4", state.models[0].id)
        assertEquals("GPT-4o", state.models[1].name)
        assertFalse(state.isLoadingModels)
    }
}
