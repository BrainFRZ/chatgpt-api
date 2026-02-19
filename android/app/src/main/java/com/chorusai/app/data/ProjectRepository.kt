package com.chorusai.app.data

import com.chorusai.app.model.*
import com.chorusai.app.network.ChorusApi
import retrofit2.Response
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class ProjectRepository @Inject constructor(
    private val api: ChorusApi
) {
    suspend fun createProject(
        username: String,
        projectName: String
    ): Response<Map<String, String>> =
        api.createProject(CreateProjectRequest(username, projectName))

    suspend fun renameProject(
        username: String,
        oldName: String,
        newName: String
    ): Response<Map<String, String>> =
        api.renameProject(RenameProjectRequest(username, oldName, newName))

    suspend fun deleteProject(
        username: String,
        projectName: String
    ): Response<Map<String, String>> =
        api.deleteProject(DeleteProjectRequest(username, projectName))

    suspend fun getProjectMetadata(
        username: String,
        project: String
    ): Response<ProjectMetadata> =
        api.getProjectMetadata(username, project)

    suspend fun getProjectChats(
        username: String,
        project: String,
        limit: Int = 20,
        offset: Int = 0
    ): Response<ProjectChatsResponse> =
        api.getProjectChats(username, project, limit, offset)

    suspend fun getProjectChatsDetailed(
        username: String,
        project: String,
        limit: Int = 50,
        offset: Int = 0
    ): Response<ProjectChatsDetailedResponse> =
        api.getProjectChatsDetailed(username, project, limit, offset)

    suspend fun getProjectFiles(
        username: String,
        project: String,
        model: String? = null
    ): Response<ProjectFilesResponse> =
        api.getProjectFiles(username, project, model)

    suspend fun updateFileStaged(
        username: String,
        project: String,
        filename: String,
        staged: Boolean
    ): Response<Map<String, Any>> =
        api.updateFileStaged(username, project, filename, UpdateStagedRequest(staged))

    suspend fun updateFileAgents(
        username: String,
        project: String,
        filename: String,
        agents: List<String>
    ): Response<Map<String, Any>> =
        api.updateFileAgents(username, project, filename, UpdateFileAgentsRequest(agents))

    suspend fun deleteProjectFile(
        username: String,
        project: String,
        filename: String
    ): Response<Map<String, String>> =
        api.deleteProjectFile(username, project, filename)

    suspend fun getProjectInstructions(
        username: String,
        project: String,
        model: String? = null
    ): Response<ProjectInstructionsResponse> =
        api.getProjectInstructions(username, project, model)

    suspend fun updateProjectInstructions(
        username: String,
        project: String,
        instructions: String
    ): Response<Map<String, Any>> =
        api.updateProjectInstructions(username, project, UpdateInstructionsRequest(instructions))

    suspend fun getAgentInstructions(
        username: String,
        project: String
    ): Response<Map<String, ProjectInstructionsResponse>> =
        api.getAgentInstructions(username, project)

    suspend fun updateAgentInstructions(
        username: String,
        project: String,
        agentName: String,
        instructions: String
    ): Response<Map<String, Any>> =
        api.updateAgentInstructions(username, project, agentName, UpdateInstructionsRequest(instructions))
}
