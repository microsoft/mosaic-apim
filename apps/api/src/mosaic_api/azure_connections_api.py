from typing import cast

from fastapi import APIRouter, Request, Response, status

from mosaic_api.api import Admin, _actor
from mosaic_api.azure_connections import (
    AzureConnectionCreate,
    AzureConnectionSecret,
    AzureConnectionService,
    AzureConnectionUpdate,
    AzureConnectionView,
)

azure_connections_router = APIRouter(prefix="/api/v1/azure-connections", tags=["admin"])


def _connections(request: Request) -> AzureConnectionService:
    return cast(AzureConnectionService, request.app.state.azure_connection_service)


@azure_connections_router.get("")
async def list_connections(request: Request, auth: Admin) -> list[AzureConnectionView]:
    return await _connections(request).list(_actor(auth))


@azure_connections_router.post("", status_code=status.HTTP_201_CREATED)
async def create_connection(
    request: Request, auth: Admin, payload: AzureConnectionCreate
) -> AzureConnectionView:
    return await _connections(request).create(_actor(auth), payload)


@azure_connections_router.patch("/{connection_id}")
async def rename_connection(
    request: Request, auth: Admin, connection_id: str, payload: AzureConnectionUpdate
) -> AzureConnectionView:
    return await _connections(request).rename(_actor(auth), connection_id, payload)


@azure_connections_router.post("/{connection_id}/test")
async def test_connection(request: Request, auth: Admin, connection_id: str) -> AzureConnectionView:
    return await _connections(request).test(_actor(auth), connection_id)


@azure_connections_router.put("/{connection_id}/secret")
async def replace_secret(
    request: Request, auth: Admin, connection_id: str, payload: AzureConnectionSecret
) -> AzureConnectionView:
    return await _connections(request).replace_secret(_actor(auth), connection_id, payload)


@azure_connections_router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(request: Request, auth: Admin, connection_id: str) -> Response:
    await _connections(request).delete(_actor(auth), connection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
