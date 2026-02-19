package com.chorusai.app.ui.navigation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ScreenTest {

    // ---- Static routes ----

    @Test
    fun `Login route is correct`() {
        assertEquals("login", Screen.Login.route)
    }

    @Test
    fun `ChatList route is correct`() {
        assertEquals("chat_list", Screen.ChatList.route)
    }

    @Test
    fun `Settings route is correct`() {
        assertEquals("settings", Screen.Settings.route)
    }

    // ---- Chat route ----

    @Test
    fun `Chat route template is correct`() {
        assertEquals("chat/{chatName}?project={project}", Screen.Chat.route)
    }

    @Test
    fun `Chat createRoute without project`() {
        assertEquals("chat/my-chat", Screen.Chat.createRoute("my-chat"))
    }

    @Test
    fun `Chat createRoute with project`() {
        assertEquals(
            "chat/my-chat?project=myProject",
            Screen.Chat.createRoute("my-chat", "myProject")
        )
    }

    @Test
    fun `Chat createRoute with null project`() {
        assertEquals("chat/test", Screen.Chat.createRoute("test", null))
    }

    @Test
    fun `Chat createRoute with special characters in name`() {
        assertEquals("chat/chat-2024", Screen.Chat.createRoute("chat-2024"))
    }

    @Test
    fun `Chat createRoute encodes slashes`() {
        val route = Screen.Chat.createRoute("my/chat")
        assertTrue(route.contains("%2F") || route.contains("%2f"))
        assertTrue(route.startsWith("chat/"))
    }

    @Test
    fun `Chat createRoute encodes question marks`() {
        val route = Screen.Chat.createRoute("what?")
        assertTrue(route.contains("%3F") || route.contains("%3f"))
    }

    @Test
    fun `Chat createRoute encodes spaces`() {
        val route = Screen.Chat.createRoute("my chat")
        assertTrue(route.contains("%20"))
    }

    @Test
    fun `Chat createRoute encodes project with special chars`() {
        val route = Screen.Chat.createRoute("chat1", "my project")
        assertTrue(route.contains("project=my%20project"))
    }

    // ---- ProjectLanding route ----

    @Test
    fun `ProjectLanding route template is correct`() {
        assertEquals("project/{projectName}", Screen.ProjectLanding.route)
    }

    @Test
    fun `ProjectLanding createRoute`() {
        assertEquals("project/myProject", Screen.ProjectLanding.createRoute("myProject"))
    }

    @Test
    fun `ProjectLanding createRoute encodes special chars`() {
        val route = Screen.ProjectLanding.createRoute("my project/v2")
        assertTrue(route.contains("%20"))
        assertTrue(route.contains("%2F") || route.contains("%2f"))
    }

    // ---- ApiKeySetup route ----

    @Test
    fun `ApiKeySetup route template is correct`() {
        assertEquals(
            "api_key_setup/{username}?isNewUser={isNewUser}",
            Screen.ApiKeySetup.route
        )
    }

    @Test
    fun `ApiKeySetup createRoute with default isNewUser`() {
        assertEquals(
            "api_key_setup/testuser?isNewUser=true",
            Screen.ApiKeySetup.createRoute("testuser")
        )
    }

    @Test
    fun `ApiKeySetup createRoute with isNewUser true`() {
        assertEquals(
            "api_key_setup/alice?isNewUser=true",
            Screen.ApiKeySetup.createRoute("alice", true)
        )
    }

    @Test
    fun `ApiKeySetup createRoute with isNewUser false`() {
        assertEquals(
            "api_key_setup/bob?isNewUser=false",
            Screen.ApiKeySetup.createRoute("bob", false)
        )
    }
}
