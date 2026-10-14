using Microsoft.AspNetCore.Mvc.ModelBinding;
using Microsoft.AspNetCore.Mvc;

namespace GrievanceApi.Helpers;

/// <summary>
/// Binds every uploaded file, whatever its form field name (MVC's own binder only takes the files
/// sent under the property's name). Swagger still shows the field as a file upload.
/// </summary>
public sealed class AllFormFilesAttribute : ModelBinderAttribute
{
    public AllFormFilesAttribute() : base(typeof(AllFormFilesBinder)) { }

    public override BindingSource BindingSource => BindingSource.FormFile;
}

sealed class AllFormFilesBinder : IModelBinder
{
    public async Task BindModelAsync(ModelBindingContext context)
    {
        var request = context.HttpContext.Request;
        context.Result = request.HasFormContentType
            ? ModelBindingResult.Success((await request.ReadFormAsync(context.HttpContext.RequestAborted)).Files)
            : ModelBindingResult.Success(null);
    }
}
