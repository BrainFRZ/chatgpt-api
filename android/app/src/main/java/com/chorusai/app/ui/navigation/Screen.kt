package com.chorusai.app.ui.navigation

import java.net.URLEncoder

private fun encode(value: String): String =
    URLEncoder.encode(value, Charsets.UTF_8.name()).replace("+", "%20")

sealed class Screen(val route: String) {
    data object Login : Screen("login")
    data object ChatList : Screen("chat_list")
    data object Chat : Screen("chat/{chatName}?project={project}") {
        fun createRoute(chatName: String, project: String? = null): String {
            val encoded = encode(chatName)
            return if (project != null) "chat/$encoded?project=${encode(project)}"
            else "chat/$encoded"
        }
    }
    data object ProjectLanding : Screen("project/{projectName}") {
        fun createRoute(projectName: String): String = "project/${encode(projectName)}"
    }
    data object ApiKeySetup : Screen("api_key_setup/{username}?isNewUser={isNewUser}") {
        fun createRoute(username: String, isNewUser: Boolean = true): String =
            "api_key_setup/${encode(username)}?isNewUser=$isNewUser"
    }
    data object Settings : Screen("settings")
}
