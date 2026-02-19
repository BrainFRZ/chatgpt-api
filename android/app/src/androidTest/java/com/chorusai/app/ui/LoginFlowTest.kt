package com.chorusai.app.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.navigation.compose.rememberNavController
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chorusai.app.ui.screens.LoginScreen
import com.chorusai.app.ui.theme.ChorusTheme
import dagger.hilt.android.testing.HiltAndroidRule
import dagger.hilt.android.testing.HiltAndroidTest
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@HiltAndroidTest
@RunWith(AndroidJUnit4::class)
class LoginFlowTest {

    @get:Rule(order = 0)
    val hiltRule = HiltAndroidRule(this)

    @get:Rule(order = 1)
    val composeRule = createComposeRule()

    @Before
    fun setup() {
        hiltRule.inject()
    }

    @Test
    fun loginScreen_displaysTitle() {
        composeRule.setContent {
            ChorusTheme {
                LoginScreen(navController = rememberNavController())
            }
        }

        composeRule.onNodeWithText("Chorus AI").assertIsDisplayed()
    }

    @Test
    fun loginScreen_displaysUsernameField() {
        composeRule.setContent {
            ChorusTheme {
                LoginScreen(navController = rememberNavController())
            }
        }

        composeRule.onNodeWithText("Username").assertIsDisplayed()
    }

    @Test
    fun loginScreen_displaysLoginButton() {
        composeRule.setContent {
            ChorusTheme {
                LoginScreen(navController = rememberNavController())
            }
        }

        composeRule.onNodeWithText("Login").assertIsDisplayed()
    }

    @Test
    fun loginButton_disabledWhenUsernameEmpty() {
        composeRule.setContent {
            ChorusTheme {
                LoginScreen(navController = rememberNavController())
            }
        }

        composeRule.onNodeWithText("Login").assertIsNotEnabled()
    }

    @Test
    fun loginButton_enabledWhenUsernameEntered() {
        composeRule.setContent {
            ChorusTheme {
                LoginScreen(navController = rememberNavController())
            }
        }

        composeRule.onNodeWithText("Username").performTextInput("testuser")
        composeRule.onNodeWithText("Login").assertIsEnabled()
    }

    @Test
    fun loginScreen_displaysHelpText() {
        composeRule.setContent {
            ChorusTheme {
                LoginScreen(navController = rememberNavController())
            }
        }

        composeRule.onNodeWithText("Letters and numbers only").assertIsDisplayed()
    }

    @Test
    fun loginScreen_showsErrorForShortUsername() {
        composeRule.setContent {
            ChorusTheme {
                LoginScreen(navController = rememberNavController())
            }
        }

        composeRule.onNodeWithText("Username").performTextInput("a")
        composeRule.onNodeWithText("Login").performClick()
        composeRule.waitForIdle()

        composeRule.onNodeWithText("Username must be at least 2 characters").assertIsDisplayed()
    }
}
