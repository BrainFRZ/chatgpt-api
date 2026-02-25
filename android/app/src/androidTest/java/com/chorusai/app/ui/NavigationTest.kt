package com.chorusai.app.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chorusai.app.ui.navigation.NavGraph
import com.chorusai.app.ui.theme.ChorusTheme
import dagger.hilt.android.testing.HiltAndroidRule
import dagger.hilt.android.testing.HiltAndroidTest
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@HiltAndroidTest
@RunWith(AndroidJUnit4::class)
class NavigationTest {

    @get:Rule(order = 0)
    val hiltRule = HiltAndroidRule(this)

    @get:Rule(order = 1)
    val composeRule = createComposeRule()

    @Before
    fun setup() {
        hiltRule.inject()
    }

    @Test
    fun appStartsOnLoginScreen() {
        composeRule.setContent {
            ChorusTheme {
                NavGraph()
            }
        }

        // The login screen should be visible (either the loading spinner or the login form)
        // After auto-login check completes and fails, the login form should show
        composeRule.waitForIdle()
        composeRule.onNodeWithText("Chorus AI").assertIsDisplayed()
    }

    @Test
    fun loginScreenDisplaysAllElements() {
        composeRule.setContent {
            ChorusTheme {
                NavGraph()
            }
        }

        composeRule.waitForIdle()
        composeRule.onNodeWithText("Chorus AI").assertIsDisplayed()
        composeRule.onNodeWithText("Username").assertIsDisplayed()
        composeRule.onNodeWithText("Login").assertIsDisplayed()
        composeRule.onNodeWithText("Letters and numbers only").assertIsDisplayed()
    }
}
