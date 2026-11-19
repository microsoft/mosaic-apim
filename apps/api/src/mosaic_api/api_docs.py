from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

RUNTIME_ASSET_POLICY = (
    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; img-src 'self' data: blob:; "
    "worker-src 'self' blob:; media-src 'self' data: blob:; manifest-src 'self'; "
    "object-src 'none'"
)


def install_api_docs(app: FastAPI) -> None:
    app.mount(
        "/docs-assets",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="docs-assets",
    )

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        return get_swagger_ui_html(
            openapi_url=f"{root}{app.openapi_url}",
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=f"{root}/docs/oauth2-redirect",
            swagger_js_url=f"{root}/docs-assets/swagger-ui-5.34.1/swagger-ui-bundle.js",
            swagger_css_url=f"{root}/docs-assets/swagger-ui-5.34.1/swagger-ui.css",
            swagger_favicon_url=f"{root}/docs-assets/favicon.svg",
            swagger_ui_parameters={"validatorUrl": None},
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_oauth_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    async def redoc(request: Request) -> HTMLResponse:
        root = request.scope.get("root_path", "").rstrip("/")
        html = get_redoc_html(
            openapi_url=f"{root}{app.openapi_url}",
            title=f"{app.title} - ReDoc",
            redoc_js_url=f"{root}/docs-assets/redoc-2.5.4/redoc.standalone.js",
            redoc_favicon_url=f"{root}/docs-assets/favicon.svg",
            with_google_fonts=False,
        )
        return HTMLResponse(
            bytes(html.body).decode().replace(
                "</head>",
                f'<link rel="stylesheet" href="{root}/docs-assets/docs-fonts.css"></head>',
            )
        )
