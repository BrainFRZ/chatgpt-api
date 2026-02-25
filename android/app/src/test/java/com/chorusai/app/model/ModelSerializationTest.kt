package com.chorusai.app.model

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class ModelSerializationTest {

    private lateinit var gson: Gson

    @Before
    fun setup() {
        gson = GsonBuilder().create()
    }

    // ---- Auth models ----

    @Test
    fun `LoginRequest serializes correctly`() {
        val request = LoginRequest("testuser")
        val json = gson.toJson(request)
        assertEquals("""{"username":"testuser"}""", json)
    }

    @Test
    fun `LoginResponse deserializes from snake_case`() {
        val json = """{"username":"testuser","has_api_key":true,"is_new_user":false}"""
        val response = gson.fromJson(json, LoginResponse::class.java)
        assertEquals("testuser", response.username)
        assertTrue(response.hasApiKey)
        assertFalse(response.isNewUser)
    }

    @Test
    fun `LoginResponse deserializes new user`() {
        val json = """{"username":"newbie","has_api_key":false,"is_new_user":true}"""
        val response = gson.fromJson(json, LoginResponse::class.java)
        assertEquals("newbie", response.username)
        assertFalse(response.hasApiKey)
        assertTrue(response.isNewUser)
    }

    @Test
    fun `ApiKeysRequest serializes with snake_case keys`() {
        val request = ApiKeysRequest("testuser", "sk-abc", "sk-ant")
        val json = gson.toJson(request)
        assertTrue(json.contains("\"openai_key\":\"sk-abc\""))
        assertTrue(json.contains("\"anthropic_key\":\"sk-ant\""))
        assertTrue(json.contains("\"username\":\"testuser\""))
    }

    @Test
    fun `ApiKeysRequest serializes null keys correctly`() {
        val request = ApiKeysRequest("testuser", null, null)
        val json = gson.toJson(request)
        assertTrue(json.contains("\"username\":\"testuser\""))
        // Gson by default omits nulls
        assertFalse(json.contains("openai_key"))
        assertFalse(json.contains("anthropic_key"))
    }

    @Test
    fun `ApiKeysResponse deserializes correctly`() {
        val json = """{"has_openai":true,"has_anthropic":false}"""
        val response = gson.fromJson(json, ApiKeysResponse::class.java)
        assertTrue(response.hasOpenai)
        assertFalse(response.hasAnthropic)
    }

    @Test
    fun `ApiKeyRequest serializes correctly`() {
        val request = ApiKeyRequest("testuser", "sk-12345")
        val json = gson.toJson(request)
        assertTrue(json.contains("\"api_key\":\"sk-12345\""))
        assertTrue(json.contains("\"username\":\"testuser\""))
    }

    // ---- Chat models ----

    @Test
    fun `ChatListResponse deserializes correctly`() {
        val json = """{
            "chats": ["chat1", "chat2"],
            "projects": ["proj1"],
            "total": 3,
            "has_more": false
        }"""
        val response = gson.fromJson(json, ChatListResponse::class.java)
        assertEquals(listOf("chat1", "chat2"), response.chats)
        assertEquals(listOf("proj1"), response.projects)
        assertEquals(3, response.total)
        assertFalse(response.hasMore)
    }

    @Test
    fun `ChatListResponse with has_more true`() {
        val json = """{"chats":[],"projects":[],"total":50,"has_more":true}"""
        val response = gson.fromJson(json, ChatListResponse::class.java)
        assertTrue(response.hasMore)
        assertEquals(50, response.total)
    }

    @Test
    fun `ChatMessage deserializes with all fields`() {
        val json = """{
            "id": "msg-123",
            "parent_id": "msg-122",
            "role": "assistant",
            "content": "Hello!",
            "timestamp": "2024-01-01T00:00:00",
            "tokens": "150",
            "cost": "0.001",
            "model": "gpt-4",
            "bookmark": "important",
            "total_tokens": 500,
            "service_tier": "flex"
        }"""
        val msg = gson.fromJson(json, ChatMessage::class.java)
        assertEquals("msg-123", msg.id)
        assertEquals("msg-122", msg.parentId)
        assertEquals("assistant", msg.role)
        assertEquals("Hello!", msg.content)
        assertEquals("150", msg.tokens)
        assertEquals("0.001", msg.cost)
        assertEquals("gpt-4", msg.model)
        assertEquals("important", msg.bookmark)
        assertEquals(500, msg.totalTokens)
        assertEquals("flex", msg.serviceTier)
    }

    @Test
    fun `ChatMessage deserializes with minimal fields`() {
        val json = """{"role":"user","content":"Hi"}"""
        val msg = gson.fromJson(json, ChatMessage::class.java)
        assertEquals("user", msg.role)
        assertEquals("Hi", msg.content)
        assertNull(msg.id)
        assertNull(msg.parentId)
        assertNull(msg.model)
    }

    @Test
    fun `ChatMessage with attached_files`() {
        val json = """{
            "role": "user",
            "content": "See file",
            "attached_files": [{"filename": "test.txt", "content": "hello"}]
        }"""
        val msg = gson.fromJson(json, ChatMessage::class.java)
        assertEquals(1, msg.attachedFiles?.size)
        assertEquals("test.txt", msg.attachedFiles?.first()?.filename)
        assertEquals("hello", msg.attachedFiles?.first()?.content)
    }

    @Test
    fun `CreateChatRequest serializes correctly`() {
        val request = CreateChatRequest("testuser", "my-chat", "project1", "gpt-4")
        val json = gson.toJson(request)
        assertTrue(json.contains("\"chat_name\":\"my-chat\""))
        assertTrue(json.contains("\"project\":\"project1\""))
        assertTrue(json.contains("\"model\":\"gpt-4\""))
    }

    @Test
    fun `SendMessageRequest serializes correctly`() {
        val request = SendMessageRequest(
            username = "testuser",
            chatName = "chat1",
            message = "Hello",
            project = "proj",
            parentId = "msg-1"
        )
        val json = gson.toJson(request)
        assertTrue(json.contains("\"chat_name\":\"chat1\""))
        assertTrue(json.contains("\"parent_id\":\"msg-1\""))
    }

    @Test
    fun `MessageResponse deserializes correctly`() {
        val json = """{
            "assistant_message": "Hi there!",
            "tokens": "200",
            "cost": "0.003",
            "context_start_index": 5,
            "user_message_id": "u-1",
            "assistant_message_id": "a-1",
            "current_leaf_id": "leaf-1",
            "total_messages": 10,
            "model": "gpt-4"
        }"""
        val response = gson.fromJson(json, MessageResponse::class.java)
        assertEquals("Hi there!", response.assistantMessage)
        assertEquals("200", response.tokens)
        assertEquals(5, response.contextStartIndex)
        assertEquals("u-1", response.userMessageId)
        assertEquals("a-1", response.assistantMessageId)
        assertEquals("gpt-4", response.model)
    }

    @Test
    fun `ChatSummary deserializes correctly`() {
        val json = """{
            "name": "chat1",
            "last_message": "Hello",
            "last_active": "2024-01-01",
            "message_count": 42,
            "cost": 0.55
        }"""
        val summary = gson.fromJson(json, ChatSummary::class.java)
        assertEquals("chat1", summary.name)
        assertEquals("Hello", summary.lastMessage)
        assertEquals(42, summary.messageCount)
        assertEquals(0.55, summary.cost, 0.001)
    }

    @Test
    fun `RenameChatRequest serializes correctly`() {
        val request = RenameChatRequest("testuser", "old", "new", "proj")
        val json = gson.toJson(request)
        assertTrue(json.contains("\"old_name\":\"old\""))
        assertTrue(json.contains("\"new_name\":\"new\""))
    }

    @Test
    fun `DeleteChatRequest serializes correctly`() {
        val request = DeleteChatRequest("testuser", "chat1", "proj")
        val json = gson.toJson(request)
        assertTrue(json.contains("\"chat_name\":\"chat1\""))
    }

    @Test
    fun `ChatResponse deserializes correctly`() {
        val json = """{
            "messages": [{"role":"user","content":"Hi"}],
            "total_messages": 1,
            "has_more_messages": false,
            "current_leaf_id": "leaf-1",
            "model": "gpt-4",
            "anthropic_sync": true,
            "game_system": "dnd5e"
        }"""
        val response = gson.fromJson(json, ChatResponse::class.java)
        assertEquals(1, response.messages.size)
        assertEquals(1, response.totalMessages)
        assertFalse(response.hasMoreMessages)
        assertEquals("leaf-1", response.currentLeafId)
        assertEquals("gpt-4", response.model)
        assertTrue(response.anthropicSync == true)
        assertEquals("dnd5e", response.gameSystem)
    }

    // ---- Project models ----

    @Test
    fun `CreateProjectRequest serializes correctly`() {
        val request = CreateProjectRequest("testuser", "myProject")
        val json = gson.toJson(request)
        assertTrue(json.contains("\"project_name\":\"myProject\""))
    }

    @Test
    fun `ProjectMetadata deserializes correctly`() {
        val json = """{
            "last_accessed": "2024-01-01",
            "model": "gpt-4",
            "game_system": "coc7e"
        }"""
        val metadata = gson.fromJson(json, ProjectMetadata::class.java)
        assertEquals("2024-01-01", metadata.lastAccessed)
        assertEquals("gpt-4", metadata.model)
        assertEquals("coc7e", metadata.gameSystem)
    }

    @Test
    fun `ProjectFileInfo deserializes correctly`() {
        val json = """{
            "filename": "doc.md",
            "tokens": 500,
            "size_bytes": 2048,
            "staged": true,
            "agents": ["events", "narration"]
        }"""
        val info = gson.fromJson(json, ProjectFileInfo::class.java)
        assertEquals("doc.md", info.filename)
        assertEquals(500, info.tokens)
        assertEquals(2048, info.sizeBytes)
        assertTrue(info.staged)
        assertEquals(listOf("events", "narration"), info.agents)
    }

    @Test
    fun `ProjectFilesResponse deserializes correctly`() {
        val json = """{
            "files": [],
            "total_tokens": 1000,
            "staged_tokens": 500
        }"""
        val response = gson.fromJson(json, ProjectFilesResponse::class.java)
        assertTrue(response.files.isEmpty())
        assertEquals(1000, response.totalTokens)
        assertEquals(500, response.stagedTokens)
    }

    // ---- Branching models ----

    @Test
    fun `BranchInfoResponse deserializes correctly`() {
        val json = """{
            "siblings": [{"id":"s1","index":0,"total":2},{"id":"s2","index":1,"total":2}],
            "current_index": 0,
            "total_siblings": 2
        }"""
        val response = gson.fromJson(json, BranchInfoResponse::class.java)
        assertEquals(2, response.siblings.size)
        assertEquals("s1", response.siblings[0].id)
        assertEquals(0, response.currentIndex)
        assertEquals(2, response.totalSiblings)
    }

    // ---- GameSystem models ----

    @Test
    fun `GameSystemInfo deserializes correctly`() {
        val json = """{"id":"dnd5e","name":"D&D 5th Edition"}"""
        val info = gson.fromJson(json, GameSystemInfo::class.java)
        assertEquals("dnd5e", info.id)
        assertEquals("D&D 5th Edition", info.name)
    }

    @Test
    fun `SetProjectGameSystemRequest serializes correctly`() {
        val request = SetProjectGameSystemRequest("testuser", "proj1", "coc7e")
        val json = gson.toJson(request)
        assertTrue(json.contains("\"game_system\":\"coc7e\""))
    }

    // ---- PipelineState models ----

    @Test
    fun `PipelineState defaults are correct`() {
        val state = PipelineState()
        assertEquals(1, state.pacing.episode)
        assertEquals(1, state.pacing.beat)
        assertEquals(0, state.pacing.responses)
        assertEquals(1, state.callbackLedger.nextId)
        assertTrue(state.callbackLedger.open.isEmpty())
        assertTrue(state.npcMemories.isEmpty())
        assertTrue(state.sceneState.pcsPresent.isEmpty())
        assertTrue(state.sceneState.npcsPresent.isEmpty())
        assertTrue(state.characterStates.isEmpty())
        assertEquals(0, state.turnCounter)
    }

    @Test
    fun `PipelineState deserializes from JSON`() {
        val json = """{
            "pacing": {"episode": 3, "beat": 2, "responses": 15},
            "callback_ledger": {"next_id": 5, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {"pcs_present": [], "npcs_present": []},
            "character_states": {},
            "game_state": {},
            "turn_counter": 10
        }"""
        val state = gson.fromJson(json, PipelineState::class.java)
        // Gson deserializes ints as Double in Any? fields — toCleanString() handles display
        assertEquals(3.0, state.pacing.episode)
        assertEquals(2.0, state.pacing.beat)
        assertEquals("3", state.pacing.episode.toCleanString())
        assertEquals("2", state.pacing.beat.toCleanString())
        assertEquals(15, state.pacing.responses)
        assertEquals(5, state.callbackLedger.nextId)
        assertEquals(10, state.turnCounter)
    }

    // ---- Stats models ----

    @Test
    fun `FreeTokensResponse deserializes correctly`() {
        val json = """{
            "total_free": 100000,
            "used": 50000,
            "remaining": 50000,
            "resets_at_eastern": "2024-01-02T00:00:00"
        }"""
        val response = gson.fromJson(json, FreeTokensResponse::class.java)
        assertEquals(100000, response.totalFree)
        assertEquals(50000, response.used)
        assertEquals(50000, response.remaining)
    }

    // ---- Model pricing ----

    @Test
    fun `ModelInfo deserializes correctly`() {
        val json = """{
            "id": "gpt-4",
            "name": "GPT-4",
            "pricing": {"input_new": 0.03, "input_cached": 0.015, "output": 0.06, "reasoning": 0.0},
            "context_limits": {"threshold": 128000, "target": 100000}
        }"""
        val info = gson.fromJson(json, ModelInfo::class.java)
        assertEquals("gpt-4", info.id)
        assertEquals("GPT-4", info.name)
        assertEquals(0.03, info.pricing.inputNew, 0.001)
        assertEquals(128000, info.contextLimits.threshold)
    }
}
