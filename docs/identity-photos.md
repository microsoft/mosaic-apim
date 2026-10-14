# Optional Entra person photos

The administrator console can read an ordinary Entra user's thumbnail through MOSAIC, including
the signed-in administrator's own photo. Use the actual tenant-local Entra object ID (`oid`),
not a MOSAIC principal ID, application ID or MSAL home-account ID. A user need not have been
onboarded in MOSAIC. Agents (including agent users), applications, managed identities and groups
use type icons instead.

## Consent and configuration

Photos use the API's existing Azure credential: its managed identity in Azure, not the
administrator's delegated Microsoft Graph token. The deployment must use Entra authentication
and have `MOSAIC_ENTRA_DIRECTORY_LOOKUP` enabled. Configuration alone does not prove consent.

Microsoft Graph requires separately administrator-consented **application**
`ProfilePhoto.Read.All` for user photos. The existing `User.ReadBasic.All` permission verifies
the user object but is not sufficient for app-only photos. See Microsoft's
[Get profilePhoto documentation](https://learn.microsoft.com/en-us/graph/api/profilephoto-get?view=graph-rest-1.0).
Granting this permission is a separate tenant-administrator decision; MOSAIC never grants it,
changes permissions, writes to Graph or changes the tenant. No additional group or application
read permission is needed for this photo path.

Missing photo consent does not disable directory search, verification or onboarding.
Application and managed-identity registrations remain reference-only.

## Endpoint contract

`GET /api/v1/directory/objects/{object_id}/photo` requires MOSAIC's administrator role.
The authenticated caller must belong to the configured deployment tenant. The API also checks
the app-only Graph credential's tenant before making a request. No tenant selector, external URL,
access token or image-size parameter is accepted.

| Result | HTTP status | Response |
| --- | --- | --- |
| Photo | 200 | Binary `image/jpeg` or `image/png` |
| Missing user/photo or unsupported kind | 404 | Existing `not_found` coded error |
| Directory lookup off | 409 | `directory_disabled` |
| Missing authentication | 401 | Existing bearer authentication response |
| Caller lacks administrator role or belongs to another tenant | 403 | Existing authorization response |
| Graph refuses consent/credential | 403 | `directory_forbidden`; message names the permission when Graph denied a read |
| Graph throttles the read | 429 | `directory_throttled`, `details.retryAfter` and `Retry-After` (1–300 seconds) |
| Invalid object ID | 422 | `validation_error` |
| Unavailable Graph or invalid/oversized image | 502 | `directory_unavailable` |

Graph is asked for the fixed `96x96` thumbnail after a read-only `/users/{id}` kind check.
JPEG/PNG images must decode successfully, be static, have dimensions no larger than 256 pixels
per edge and have a body no larger than 64 KiB. Other types (including SVG/HTML), malformed or
truncated data, compressed HTTP responses and redirects are rejected. Streaming is stopped at
the byte limit, with a 10-second HTTP timeout and a 30-second overall fetch deadline.
429/503 responses are retried at most once per Graph read, with a wait capped at five seconds.

Responses use `Cache-Control: no-store, private` and `X-Content-Type-Options: nosniff`.
The Graph reader holds at most 128 tenant/object-scoped entries in process memory (at most
8 MiB of photo bodies); successful photos expire after two minutes and missing photos after
30 seconds. Four fixed locks coalesce identical requests and bound concurrent photo fetches.
Errors are not cached. Expired entries are purged on the next read, and shutdown clears the cache.
There is no database or filesystem persistence, and no shared/CDN cache.

The frontend should fetch a blob with its normal authenticated API client, never put a bearer
token in an image URL, and key its own ephemeral state by account, tenant and object ID. Revoke
object URLs when no longer used or when the account changes. Fall back to initials/type icons
on **every** photo failure; photos must never block login, lists, search or onboarding.

## Fictional demo fixtures

`FakeDirectoryLookup(objects, photos={object_id: DirectoryPhoto(content=bytes,
content_type="image/png")}, tenant_id="demo-tenant")` supports synthetic thumbnail fixtures.
Import `DirectoryPhoto` from `mosaic_api.integrations.graph`. The fake defaults to `tenant-test`;
set `tenant_id` to the demo API's tenant. Its public `photos` dictionary can be updated with
lowercase object-ID keys. Only ordinary `user` objects yield photos, and the same image validator
rejects malformed fixture bytes. Keep every fixture fictional; the production reader never writes
photo files.
