package com.chorusai.app.ui.navigation

import androidx.compose.runtime.Composable
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.chorusai.app.ui.screens.*

@Composable
fun NavGraph(navController: NavHostController = rememberNavController()) {
    NavHost(
        navController = navController,
        startDestination = Screen.Login.route
    ) {
        composable(Screen.Login.route) {
            LoginScreen(navController = navController)
        }

        composable(Screen.ChatList.route) {
            ChatListScreen(navController = navController)
        }

        composable(
            route = Screen.Chat.route,
            arguments = listOf(
                navArgument("chatName") { type = NavType.StringType },
                navArgument("project") {
                    type = NavType.StringType
                    nullable = true
                    defaultValue = null
                }
            )
        ) { backStackEntry ->
            ChatScreen(
                chatName = backStackEntry.arguments?.getString("chatName") ?: "",
                project = backStackEntry.arguments?.getString("project"),
                navController = navController
            )
        }

        composable(
            route = Screen.ProjectLanding.route,
            arguments = listOf(
                navArgument("projectName") { type = NavType.StringType }
            )
        ) { backStackEntry ->
            ProjectLandingScreen(
                projectName = backStackEntry.arguments?.getString("projectName") ?: "",
                navController = navController
            )
        }

        composable(Screen.Settings.route) {
            SettingsScreen(navController = navController)
        }
    }
}
