using GrievanceApi.Models;
using GrievanceApi.Services;
using Microsoft.AspNetCore.Mvc;

namespace GrievanceApi.Controllers;

[ApiController]
[Route("api/grievances")]
public sealed class GrievancesController(GrievanceCheckService service) : ControllerBase
{
    // 400 (bad upload) and 502 (OCR or LLM server failed) come from ApiExceptionHandler.
    [HttpPost("check")]
    [Consumes("multipart/form-data")]
    [ProducesResponseType<Observation>(StatusCodes.Status200OK, "application/json")]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType<ProblemDetails>(StatusCodes.Status502BadGateway, "application/problem+json")]
    [EndpointSummary("Upload a grievance folder (checked) or a single file (OCR only)")]
    [EndpointDescription(
        "files: either a grievance folder's files, each named with its path (<id>/ATR/..., <id>/Citizen/... - as a "
        + "browser's folder picker sends them), which returns the observation; or one PDF or image file, which only "
        + "returns its OCR text ({\"file_name\", \"text\"}). caseId: optional, defaults to the folder name. "
        + "Swagger can only send single files; use the test page at / to upload a folder.")]
    public async Task<IActionResult> Check([FromForm] CheckGrievanceRequest request, CancellationToken ct)
    {
        var result = await service.CheckAsync(request.CaseId, await request.ToUploadedFilesAsync(ct), ct);
        return result.Observation is not null ? Ok(result.Observation) : Ok(result.OcrText);
    }
}
