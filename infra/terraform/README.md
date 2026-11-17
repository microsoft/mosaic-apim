# Public Terraform reference deployment

[Deployment prerequisites](../../docs/deployment.md#before-you-deploy) |
[Administrator-owned private networking](../../docs/private-networking.md)

This is an alternative to Bicep/`azd up` for a **fresh, public Azure Commercial deployment**.
It does not require azd, create private networking, adopt an azd environment, or manage the
federal-demo topology. Do not point both Terraform and Bicep at the same resources.

## Prerequisites

Install Terraform 1.7+, Azure CLI, and Python 3.12. Sign in as a user with Azure provisioning
and role-assignment rights and the separate Entra permissions described in the
[prerequisite checklist](../../docs/deployment.md#before-you-deploy). Register the required
resource providers. ACR Tasks builds the existing Dockerfiles remotely; local Docker is not
required for this path. The deployer needs permission to queue builds and read image digests.

Each checkout holds **one** Terraform environment, including generated inputs and bootstrap
metadata. Use a separate checkout and state for another environment. Run the helper from the
repository root. Do not copy `.azure/` from an azd deployment.

## Deploy without azd

```powershell
az login
az account set --subscription <subscription-id>

# Explicit directory changes: applications, service principals, initial Admin assignment,
# and existing bootstrap consent behavior. This is NOT a read-only planning command.
python scripts\deploy_terraform.py bootstrap `
  --environment-name mosaic-dev `
  --subscription-id <subscription-id> `
  --publisher-name "MOSAIC administrator" `
  --publisher-email "admin@example.com"

# Review infra\terraform\mosaic.auto.tfvars.json before planning.
python scripts\deploy_terraform.py plan
python scripts\deploy_terraform.py apply

# Registers the deployed console/portal redirects and attempts baseline Graph consent.
python scripts\deploy_terraform.py configure

# Builds API, console and portal into the newly created public ACR.
python scripts\deploy_terraform.py build --python-index-url https://pypi.org/simple
python scripts\deploy_terraform.py plan
python scripts\deploy_terraform.py apply
python scripts\deploy_terraform.py check
```

`plan` initializes providers and saves `infra/terraform/mosaic.tfplan`; inspect its output.
`apply` is the explicit authorization to execute that saved plan (there is no second Terraform
approval prompt for saved plans). The helper rejects a plan after Terraform sources, the
provider lockfile, or variable files change. Neither command creates Entra objects or builds
images. Terraform's own state-locking/stale-plan checks still apply; do not run concurrent
operators or use `TF_CLI_ARGS*`/`TF_VAR_*` to override helper-managed deployment inputs.

The first apply provisions infrastructure with placeholder images. It is not a working MOSAIC
installation until the image build and second apply complete. `configure` can be repeated after
managed-identity propagation or after an administrator supplies missing directory permissions.
Consent warnings are not evidence that consent succeeded. Optional Graph write consent is
never enabled by this helper; use the documented separately authorized administration flow.

`build` stages **all three digest-pinned images** in `mosaic.auto.tfvars.json` only after all
builds succeed. The next plan/apply deploys those images. Terraform remains the image owner;
repeat applies do not restore placeholders. No `local-exec`, hidden `azd`, or image-changing
`az webapp` command is used. Do not delete the generated inputs between deployments. Builds
incur ACR Tasks charges and need outbound access to package/base-image sources; package-index
credentials must not be put into build arguments.

`check` uses `/readyz` on the API and `/healthz` on both frontends with bounded retries.
It reports URLs, not a proof of Entra sign-in, model authorization, or a full inference journey.
Complete sign-in and the [verification runbook](../../docs/verification.md) separately.

## Inputs and ownership

`example.tfvars` documents the native Terraform interface with fictional IDs. The helper
generates actual IDs using the existing `scripts/mosaic_entra.py` implementation. A small
environment-sink adapter writes non-secret configuration to
`infra/terraform/.mosaic-terraform/environment.json`, rather than calling `azd env set`.
Scope/role IDs, identity lookup rules, and optional consent defaults are shared with azd.
Existing identity ownership tags are retained for bootstrap compatibility; the `azd` tag on
these directory objects does not mean Terraform invokes azd or owns their deletion.

Terraform owns the resource group, App Service plan/sites, public Basic ACR, Cosmos account and
five containers, two RBAC vaults, workspace/Insights, APIM, role assignments, diagnostics, and
app settings. Outputs are available with:

```powershell
terraform -chdir=infra\terraform output -json deployment
```

Use the generated `mosaic.auto.tfvars.json` for infrastructure options such as
`app_service_plan_sku`, `app_service_location`, `apim_sku_name`, `tags`, `localhost_origins`,
`teams_workflow_allowed_hosts`, and `mcp_discovery_timeout_seconds`. `location` defaults to
`eastus2`. This first Terraform reference leaves private Teams bot hosting/authentication
disabled; Graph email/shared Teams setup and external integrations remain separate.

To reference an existing bootstrap gateway, set `deploy_apim=false` and
`external_bootstrap_apim_resource_id`. Terraform writes its location into the API configuration
but does **not** create permissions, loggers, or diagnostics on that gateway. An administrator
must separately arrange those and register the gateway. This is not resource adoption.

The AzureRM provider lacks the required `azureMonitor` logger and diagnostic `metrics` fields.
Only those two APIM child resources use AzAPI; other infrastructure uses native AzureRM.
Providers and hashes are committed in `.terraform.lock.hcl`.

## State and backend

The default Terraform backend is local. State and saved plans can contain sensitive resource
attributes even though MOSAIC uses managed identities and no application secrets. Generated
state, input files, and bootstrap metadata are gitignored; the Terraform directory is also
excluded from container build contexts. Store them securely, back them up, and never attach
them to a PR.

For a shared/durable deployment, **before the first plan**, have the administrator provision
an Azure Storage account/container with appropriate RBAC, network access, and state protection.
Add an operator-owned `backend.tf`:

```hcl
terraform {
  backend "azurerm" {}
}
```

Create a local, ignored `infra/terraform/backend.hcl` containing resource group, storage account,
container, and a unique state key; use Entra authentication (`use_azuread_auth = true`), not
embedded access keys. Then initialize explicitly:

```powershell
terraform -chdir=infra\terraform init -backend-config=backend.hcl
```

Subsequent helper `plan` commands reuse that backend. Review and authorize Terraform's migration
prompt if changing an existing backend. The backend is not part of this deployment and is not
deleted with it. Protect local bootstrap metadata too; restoring only remote state is not a
bootstrap/adoption mechanism.

## Updates, failures, and removal

- Retry the failed phase after correcting its reported error. Infrastructure and Entra bootstrap
  are independently resumable; no command treats a partial failure as completion.
- For app updates: `build`, review `plan`, `apply`, then `check`. Failed builds leave the previous
  image inputs unchanged. Unreferenced build images can be cleaned through normal ACR retention.
- For infrastructure updates: edit inputs, `plan`, then `apply`; rerun `configure` if application
  URLs or the API managed identity changed. Do not replace resources merely to fix propagation.
- To remove hosting, review a native destroy plan and explicitly apply it:

```powershell
terraform -chdir=infra\terraform plan -destroy "-out=remove.tfplan"
terraform -chdir=infra\terraform apply remove.tfplan
```

Destroying Terraform resources does **not** delete Entra registrations, consent, or external
resources. Review those separately; do not delete objects shared by other applications.
Both vaults have 90-day soft deletion and purge protection, and the provider does not purge
them on destroy. Immediate reuse of their names may fail or require an explicitly reviewed
recovery; never disable retention to make redeployment easier.

## Offline validation

```powershell
terraform -chdir=infra\terraform fmt -check -recursive
terraform -chdir=infra\terraform init -backend=false -input=false
terraform -chdir=infra\terraform validate
terraform -chdir=infra\terraform test "-var-file=example.tfvars"
python -m unittest scripts.tests.test_deploy_terraform
```

The Terraform tests use mocked providers: they do not authenticate, create resources, prove SKU
availability, or establish that a real tenant's policies allow the deployment.
