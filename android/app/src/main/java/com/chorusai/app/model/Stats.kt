package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class UserStatsResponse(
    // Lifetime
    @SerializedName("lifetime_prompts") val lifetimePrompts: Int,
    @SerializedName("lifetime_gpt_prompts") val lifetimeGptPrompts: Int,
    @SerializedName("lifetime_sonnet_prompts") val lifetimeSonnetPrompts: Int,
    @SerializedName("lifetime_input_tokens") val lifetimeInputTokens: Long,
    @SerializedName("lifetime_cached_tokens") val lifetimeCachedTokens: Long,
    @SerializedName("lifetime_output_tokens") val lifetimeOutputTokens: Long,
    @SerializedName("lifetime_reasoning_tokens") val lifetimeReasoningTokens: Long,
    @SerializedName("lifetime_cost") val lifetimeCost: Double,
    @SerializedName("lifetime_cache_miss_percent") val lifetimeCacheMissPercent: Double,
    // Monthly
    @SerializedName("monthly_active_days") val monthlyActiveDays: Int,
    @SerializedName("monthly_prompts") val monthlyPrompts: Int,
    @SerializedName("monthly_gpt_prompts") val monthlyGptPrompts: Int,
    @SerializedName("monthly_sonnet_prompts") val monthlySonnetPrompts: Int,
    @SerializedName("monthly_input_tokens") val monthlyInputTokens: Long,
    @SerializedName("monthly_cached_tokens") val monthlyCachedTokens: Long,
    @SerializedName("monthly_output_tokens") val monthlyOutputTokens: Long,
    @SerializedName("monthly_reasoning_tokens") val monthlyReasoningTokens: Long,
    @SerializedName("monthly_total_tokens") val monthlyTotalTokens: Long,
    @SerializedName("monthly_cost") val monthlyCost: Double,
    // Today
    @SerializedName("today_prompts") val todayPrompts: Int,
    @SerializedName("today_gpt_prompts") val todayGptPrompts: Int,
    @SerializedName("today_sonnet_prompts") val todaySonnetPrompts: Int,
    @SerializedName("today_input_tokens") val todayInputTokens: Long,
    @SerializedName("today_cached_tokens") val todayCachedTokens: Long,
    @SerializedName("today_output_tokens") val todayOutputTokens: Long,
    @SerializedName("today_reasoning_tokens") val todayReasoningTokens: Long,
    @SerializedName("today_total_tokens") val todayTotalTokens: Long,
    @SerializedName("today_cost") val todayCost: Double,
    // Averages
    @SerializedName("avg_prompts_per_day") val avgPromptsPerDay: Double,
    @SerializedName("avg_gpt_prompts_per_day") val avgGptPromptsPerDay: Double,
    @SerializedName("avg_sonnet_prompts_per_day") val avgSonnetPromptsPerDay: Double,
    @SerializedName("avg_input_per_day") val avgInputPerDay: Double,
    @SerializedName("avg_cached_per_day") val avgCachedPerDay: Double,
    @SerializedName("avg_output_per_day") val avgOutputPerDay: Double,
    @SerializedName("avg_reasoning_per_day") val avgReasoningPerDay: Double,
    @SerializedName("avg_total_per_day") val avgTotalPerDay: Double,
    @SerializedName("avg_cost_per_day") val avgCostPerDay: Double,
    @SerializedName("avg_gpt_context_growth") val avgGptContextGrowth: Double,
    @SerializedName("avg_sonnet_context_growth") val avgSonnetContextGrowth: Double,
    @SerializedName("days_since_first") val daysSinceFirst: Int
)

data class FreeTokensResponse(
    @SerializedName("total_free") val totalFree: Int,
    val used: Int,
    val remaining: Int,
    @SerializedName("resets_at_eastern") val resetsAtEastern: String
)
