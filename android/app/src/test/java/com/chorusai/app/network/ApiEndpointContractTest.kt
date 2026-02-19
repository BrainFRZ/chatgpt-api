package com.chorusai.app.network

import com.chorusai.app.model.*
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory

/**
 * Tests that the Retrofit interface correctly builds requests and parses responses
 * using a MockWebServer.
 */
class ApiEndpointContractTest {

    private lateinit var server: MockWebServer
    private lateinit var api: ChorusApi
    private lateinit var gson: Gson

    @Before
    fun setup() {
        gson = GsonBuilder().create()
        server = MockWebServer()
        server.start()

        api = Retrofit.Builder()
            .baseUrl(server.url("/"))
            .addConverterFactory(GsonConverterFactory.create(gson))
            .build()
            .create(ChorusApi::class.java)
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    // ---- Auth endpoints ----

    @Test
    fun `POST login sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"username":"alice","has_api_key":true,"is_new_user":false}""")
        )

        val response = api.login(LoginRequest("alice"))
        val request = server.takeRequest()

        assertEquals("POST", request.method)
        assertEquals("/api/login", request.path)
        assertTrue(request.body.readUtf8().contains("\"username\":\"alice\""))
        assertTrue(response.isSuccessful)
        assertEquals("alice", response.body()?.username)
        assertTrue(response.body()?.hasApiKey == true)
    }

    @Test
    fun `POST set-api-keys sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"has_openai":true,"has_anthropic":false}""")
        )

        val response = api.setApiKeys(ApiKeysRequest("alice", "sk-abc", null))
        val request = server.takeRequest()

        assertEquals("POST", request.method)
        assertEquals("/api/set-api-keys", request.path)
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"openai_key\":\"sk-abc\""))
        assertTrue(response.isSuccessful)
        assertTrue(response.body()?.hasOpenai == true)
    }

    @Test
    fun `GET api-keys sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"has_openai":false,"has_anthropic":true}""")
        )

        val response = api.getApiKeys("alice")
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertEquals("/api/api-keys/alice", request.path)
        assertTrue(response.isSuccessful)
        assertFalse(response.body()?.hasOpenai == true)
        assertTrue(response.body()?.hasAnthropic == true)
    }

    // ---- Chat endpoints ----

    @Test
    fun `GET chats sends correct request with query params`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"chats":["c1","c2"],"projects":["p1"],"total":3,"has_more":false}""")
        )

        val response = api.getChats("alice", limit = 10, offset = 5)
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertTrue(request.path!!.startsWith("/api/chats/alice"))
        assertTrue(request.path!!.contains("limit=10"))
        assertTrue(request.path!!.contains("offset=5"))
        assertTrue(response.isSuccessful)
        assertEquals(2, response.body()?.chats?.size)
    }

    @Test
    fun `POST create-chat sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"status":"created"}""")
        )

        api.createChat(CreateChatRequest("alice", "new-chat", "proj1", "gpt-4"))
        val request = server.takeRequest()

        assertEquals("POST", request.method)
        assertEquals("/api/create-chat", request.path)
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"chat_name\":\"new-chat\""))
        assertTrue(body.contains("\"project\":\"proj1\""))
    }

    @Test
    fun `GET chat sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{
                    "messages":[{"role":"user","content":"hi"}],
                    "total_messages":1,
                    "has_more_messages":false
                }""")
        )

        val response = api.getChat("alice", "chat1", project = "proj", limit = 20)
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertTrue(request.path!!.startsWith("/api/chat/alice/chat1"))
        assertTrue(request.path!!.contains("project=proj"))
        assertTrue(request.path!!.contains("limit=20"))
        assertEquals(1, response.body()?.messages?.size)
    }

    @Test
    fun `POST send-message sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{
                    "assistant_message":"Hello!",
                    "tokens":"100",
                    "cost":"0.001",
                    "context_start_index":0
                }""")
        )

        api.sendMessage(SendMessageRequest("alice", "chat1", "Hello", parentId = "msg-1"))
        val request = server.takeRequest()

        assertEquals("POST", request.method)
        assertEquals("/api/send-message", request.path)
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"parent_id\":\"msg-1\""))
    }

    @Test
    fun `POST rename-chat sends correct request`() = runTest {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"status":"ok"}"""))

        api.renameChat(RenameChatRequest("alice", "old", "new"))
        val request = server.takeRequest()

        assertEquals("POST", request.method)
        assertEquals("/api/rename-chat", request.path)
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"old_name\":\"old\""))
        assertTrue(body.contains("\"new_name\":\"new\""))
    }

    @Test
    fun `POST delete-chat sends correct request`() = runTest {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"status":"ok"}"""))

        api.deleteChat(DeleteChatRequest("alice", "chat1"))
        val request = server.takeRequest()

        assertEquals("POST", request.method)
        assertEquals("/api/delete-chat", request.path)
    }

    // ---- Branching endpoints ----

    @Test
    fun `GET branch-info sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"siblings":[],"current_index":0,"total_siblings":1}""")
        )

        api.getBranchInfo("alice", "chat1", "msg-1", "proj")
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertTrue(request.path!!.startsWith("/api/branch-info/alice/chat1"))
        assertTrue(request.path!!.contains("message_id=msg-1"))
    }

    // ---- Project endpoints ----

    @Test
    fun `POST create-project sends correct request`() = runTest {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"status":"ok"}"""))

        api.createProject(CreateProjectRequest("alice", "myProj"))
        val request = server.takeRequest()

        assertEquals("POST", request.method)
        assertEquals("/api/create-project", request.path)
        assertTrue(request.body.readUtf8().contains("\"project_name\":\"myProj\""))
    }

    @Test
    fun `GET project-metadata sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"last_accessed":"2024-01-01","model":"gpt-4","game_system":"dnd5e"}""")
        )

        val response = api.getProjectMetadata("alice", "proj1")
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertEquals("/api/project-metadata/alice/proj1", request.path)
        assertEquals("dnd5e", response.body()?.gameSystem)
    }

    @Test
    fun `GET project-files sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"files":[],"total_tokens":0,"staged_tokens":0}""")
        )

        api.getProjectFiles("alice", "proj1")
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertTrue(request.path!!.startsWith("/api/project-files/alice/proj1"))
    }

    @Test
    fun `GET project-instructions sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""{"instructions":"Be helpful","tokens":5}""")
        )

        val response = api.getProjectInstructions("alice", "proj1")
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertTrue(request.path!!.startsWith("/api/project-instructions/alice/proj1"))
        assertEquals("Be helpful", response.body()?.instructions)
    }

    // ---- Game systems endpoint ----

    @Test
    fun `GET game-systems sends correct request`() = runTest {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setBody("""[{"id":"dnd5e","name":"D&D 5E"},{"id":"coc7e","name":"CoC 7E"}]""")
        )

        val response = api.getGameSystems()
        val request = server.takeRequest()

        assertEquals("GET", request.method)
        assertEquals("/api/game-systems", request.path)
        assertEquals(2, response.body()?.size)
    }

    // ---- Error handling ----

    @Test
    fun `404 response is handled`() = runTest {
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail":"Not found"}"""))

        val response = api.login(LoginRequest("nobody"))

        assertFalse(response.isSuccessful)
        assertEquals(404, response.code())
    }

    @Test
    fun `500 response is handled`() = runTest {
        server.enqueue(MockResponse().setResponseCode(500).setBody("""{"detail":"Server error"}"""))

        val response = api.getApiKeys("alice")

        assertFalse(response.isSuccessful)
        assertEquals(500, response.code())
    }
}
