package com.chorusai.app.model

import com.google.gson.annotations.SerializedName

data class CreateProjectRequest(
    val username: String,
    @SerializedName("project_name") val projectName: String
)

data class RenameProjectRequest(
    val username: String,
    @SerializedName("old_name") val oldName: String,
    @SerializedName("new_name") val newName: String
)

data class DeleteProjectRequest(
    val username: String,
    @SerializedName("project_name") val projectName: String
)

data class ProjectChatsResponse(
    val chats: List<String>,
    val total: Int,
    @SerializedName("has_more") val hasMore: Boolean
)

data class ProjectChatsDetailedResponse(
    val chats: List<ChatSummary>,
    val total: Int,
    @SerializedName("has_more") val hasMore: Boolean
)

data class ProjectFileInfo(
    val filename: String,
    val tokens: Int,
    @SerializedName("size_bytes") val sizeBytes: Int,
    val staged: Boolean,
    val agents: List<String>
)

data class ProjectFilesResponse(
    val files: List<ProjectFileInfo>,
    @SerializedName("total_tokens") val totalTokens: Int,
    @SerializedName("staged_tokens") val stagedTokens: Int
)

data class UpdateStagedRequest(
    val staged: Boolean
)

data class UpdateFileAgentsRequest(
    val agents: List<String>
)

data class ProjectInstructionsResponse(
    val instructions: String,
    val tokens: Int
)

data class UpdateInstructionsRequest(
    val instructions: String
)

data class ProjectMetadata(
    @SerializedName("last_accessed") val lastAccessed: String,
    val model: String,
    @SerializedName("game_system") val gameSystem: String
)
