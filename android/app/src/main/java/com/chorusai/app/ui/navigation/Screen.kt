package com.chorusai.app.ui.navigation

sealed class Screen(val route: String) {
    data object Login : Screen("login")
    data object ChatList : Screen("chat_list")
    data object Chat : Screen("chat/{chatName}?project={project}") {
        fun createRoute(chatName: String, project: String? = null): String =
            if (project != null) "chat/$chatName?project=$project"
            else "chat/$chatName"
    }
    data object ProjectLanding : Screen("project/{projectName}") {
        fun createRoute(projectName: String): String = "project/$projectName"
    }
    data object Settings : Screen("settings")
}
