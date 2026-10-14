using GrievanceApi.Services;
using Microsoft.AspNetCore.Mvc;

namespace GrievanceApi.Controllers;

[ApiController]
[Route("health")]
public sealed class HealthController(HealthService service) : ControllerBase
{
    [HttpGet]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status503ServiceUnavailable)]
    [EndpointSummary("Is the API working, and can it reach the OCR and LLM servers?")]
    public async Task<IActionResult> Get(CancellationToken ct)
    {
        var report = await service.CheckAsync(ct);
        return StatusCode(report.IsHealthy ? StatusCodes.Status200OK : StatusCodes.Status503ServiceUnavailable, report);
    }
}
