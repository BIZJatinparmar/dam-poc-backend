# Azure App Service deployment guide

This guide prepares two independent container images for **two Linux App Services**. It does not deploy anything automatically. Use East US unless your subscription or existing Azure AI resources require another region. Substitute globally unique names for every `<...>` value.

## Resources and order

1. Create a resource group in `eastus`, one Linux App Service plan (start with one B2 instance), an Azure Container Registry, a private Blob Storage container named `assets`, and Azure Database for PostgreSQL Flexible Server with an empty `atlas` database. The two web apps can share the plan; the backend must remain at one instance until its analysis worker is made concurrency-safe. Enable **Always On** for the backend.
2. Restrict the PostgreSQL public firewall to the backend App Service's outbound IP addresses and require TLS. Do not select the broad "allow all Azure services" firewall rule. Put the database password in a protected App Service setting or Key Vault reference, never in either repository. Start with fresh Azure data; there is no SQLite or uploads migration.
3. Build and push the backend image to your registry, then create the backend App Service from it. Configure its system-assigned managed identity to pull from ACR (`AcrPull`) and to access the private Blob container (`Storage Blob Data Contributor`). Give it `Video Indexer Account Contributor` on the existing Video Indexer account. Set `WEBSITES_PORT=8000`.
4. Configure backend App Service settings: `DATABASE_URL=postgresql+psycopg://<user>:<password>@<server>.postgres.database.azure.com:5432/atlas?sslmode=require`, `ATLAS_STORAGE_BACKEND=blob`, `ATLAS_BLOB_ACCOUNT_URL=https://<storage>.blob.core.windows.net`, `ATLAS_BLOB_CONTAINER=assets`, `ATLAS_ALLOWED_ORIGINS=https://<frontend-app>.azurewebsites.net`, plus the existing Content Understanding and Video Indexer settings. The container runs `alembic upgrade head` before starting one Uvicorn process. Check `https://<backend-app>.azurewebsites.net/api/health`.
5. Build the frontend image with `VITE_API_BASE_URL=https://<backend-app>.azurewebsites.net`, push it to ACR, and create the frontend App Service from it. Grant its managed identity `AcrPull`; set `WEBSITES_PORT=8080`. Check the frontend root URL and upload flow. Rebuild the frontend image when its backend origin changes.

Build each image from its own Git repository:

```powershell
# In backend/
docker build -t <registry>.azurecr.io/atlas-backend:<version> .
docker push <registry>.azurecr.io/atlas-backend:<version>

# In frontend/
docker build --build-arg VITE_API_BASE_URL=https://<backend-app>.azurewebsites.net -t <registry>.azurecr.io/atlas-frontend:<version> .
docker push <registry>.azurecr.io/atlas-frontend:<version>
```

The containers listen on ports 8000 and 8080 respectively; set `WEBSITES_PORT` to match each image. [App Service custom container guidance](https://learn.microsoft.com/en-us/azure/app-service/configure-custom-container) covers port settings and managed-identity ACR pulls. [App Service managed identity](https://learn.microsoft.com/en-us/azure/app-service/overview-managed-identity) supplies the backend's Blob and Video Indexer identity endpoint. [Azure PostgreSQL firewall guidance](https://learn.microsoft.com/en-us/azure/postgresql/network/concepts-networking-public) explains the outbound-IP allowlist.

## Demo access and validation

Apply App Service access restrictions to **both** web apps so only trusted client IPs can reach them. CORS is configured for the frontend origin, but CORS does not secure the API; callers can choose any `X-Demo-User` header. Keep the Blob container private and allow media reads only through the backend. Do not use this deployment for real customer assets until authentication and authorization replace the demo switcher.

After deployment, verify the frontend loads assets; an image and MP4 upload survive a backend restart; thumbnails and video seeking work; an allowed-origin preflight succeeds; analysis reaches completion or a visible failure with Retry; and PostgreSQL plus Blob records remain after restart. Check backend App Service logs if the migration or managed-identity permissions fail.
