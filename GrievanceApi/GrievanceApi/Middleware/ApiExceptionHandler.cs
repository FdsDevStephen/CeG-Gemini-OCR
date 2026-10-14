using GrievanceApi.Exceptions;
using Microsoft.AspNetCore.Diagnostics;
using Microsoft.AspNetCore.Mvc;

namespace GrievanceApi.Middleware;

/// <summary>
/// The one place exceptions become HTTP answers:
///   InvalidUploadException -> 400 {"error": message}
///   UpstreamException      -> 502 ProblemDetails (type, title, status, detail), application/problem+json
/// Anything else is left alone (false), so it surfaces as before.
/// </summary>
public sealed class ApiExceptionHandler : IExceptionHandler
{
    public async ValueTask<bool> TryHandleAsync(HttpContext context, Exception exception, CancellationToken ct)
    {
        switch (exception)
        {
            case InvalidUploadException invalid:
                context.Response.StatusCode = StatusCodes.Status400BadRequest;
                await context.Response.WriteAsJsonAsync(new { error = invalid.Message }, ct);
                return true;

            case UpstreamException upstream:
                context.Response.StatusCode = StatusCodes.Status502BadGateway;
                var problem = new ProblemDetails
                {
                    Type = "https://tools.ietf.org/html/rfc9110#section-15.6.3",
                    Title = "Bad Gateway",
                    Status = StatusCodes.Status502BadGateway,
                    Detail = upstream.Message,
                };
                await context.Response.WriteAsJsonAsync(problem, options: null, contentType: "application/problem+json", ct);
                return true;

            default:
                return false;
        }
    }
}
