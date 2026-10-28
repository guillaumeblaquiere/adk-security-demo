# Securing Agent Actions End-to-End with Google ADK (v2.12+)

This repository contains the companion code and live demonstration for **"Secure Your Agent Actions"** (presented at **Google Cloud Builder Connect 2026**).

It demonstrates how to secure an AI agent built with the **[Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/)** when querying enterprise data in **BigQuery**, covering:
1. **Identity Delegation**: Why shared runtime Service Accounts (ADC) are dangerous for user-facing agents, and how to switch to **per-user OAuth 2.0** (Human-in-the-Loop authentication).
2. **Credential Lifecycle & Tool Architecture**: How OAuth 2.0 tokens behave across **Managed MCP (`McpToolset`)**, **Native `BigQueryToolset`**, and **Custom Tools (`GoogleTool`)**.
3. **End-to-End Token Security at Rest**: Why persisting agent sessions (e.g., in **Vertex AI Agent Engine**) exposes OAuth 2.0 tokens in plaintext by default, and how to encrypt them at rest using **Cloud KMS** (`GOOGLE_CREDENTIAL_KMS_KEY`, introduced in **ADK 2.12**).

---

## Repository Structure

```text
.
├── root_agent/
│   ├── __init__.py
│   ├── agent.py           # The 4 authentication scenarios (toggle with 1 line)
│   └── requirements.txt   # Agent-level dependencies
├── command.sh             # CLI commands to run ADK Web, inspect sessions & revoke tokens
├── requirements.txt       # Project Python dependencies (ADK >= 2.12.0)
└── README.md              # This documentation
```

---

## The 4 Authentication Scenarios (`root_agent/agent.py`)

In [`root_agent/agent.py`](root_agent/agent.py), the agent's default instruction runs:
```sql
SELECT SESSION_USER() AS current_user, 1 AS test
```
This query immediately reveals **which identity BigQuery sees** when the agent executes a tool. You can switch between the 4 scenarios by uncommenting a **single line** in `root_agent = Agent(..., tools=[...])`:

| Scenario | Tool Variable in `agent.py` | Identity Seen by BigQuery (`SESSION_USER()`) | Where OAuth2 Tokens Live | New Session (Same User) | Server Restart + Reload Session | Cloud KMS Encryption (`GOOGLE_CREDENTIAL_KMS_KEY`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Technical Identity (ADC)** | `adc_bigquery_toolset` | Runtime Service Account (`...@...iam.gserviceaccount.com`) | Local ADC / Metadata Server | No prompt (shared SA) | No prompt (shared SA) | N/A |
| **2. Managed MCP OAuth2** | `mcp_bigquery_toolset` | **End User** (`alice@example.com`) | **Server RAM** (`InMemoryCredentialService`, keyed by `user_id`) | **Still authenticated!** | **Connection lost** (must re-authenticate) | N/A (not stored in session state) |
| **3. Native `BigQueryToolset` OAuth2** | `oauth_bigquery_toolset` | **End User** (`alice@example.com`) | **`session.state["bigquery_token_cache"]`** (persisted in Agent Engine) | **Must re-authenticate** | **Still authenticated!** (reloaded from session) | **Yes** (`kms:gAAAAAB...` + `wrapped_dek`) |
| **4. Custom Tool (`GoogleTool`) OAuth2** | `custom_rest_bigquery_tool` | **End User** (`alice@example.com`) | **`session.state["custom_tool_token_cache"]`** (persisted in Agent Engine) | **Must re-authenticate** | **Still authenticated!** (reloaded from session) | **Yes** (`kms:gAAAAAB...` + `wrapped_dek`) |

---

### Deep Dive into Each Scenario

#### Scenario 1: Runtime Service Account / ADC (`adc_bigquery_toolset`)
- **What it does**: Uses `google.auth.default()` (Application Default Credentials) passed to `BigQueryCredentialsConfig(credentials=...)`.
- **What to observe**: Every user querying the agent executes SQL under the **same technical identity** (the Service Account running Cloud Run, GKE, or Agent Engine).
- **The risk**: Over-privileged agent permissions and **zero per-user audit trail** in BigQuery logs.

#### Scenario 2: End-User OAuth2 via Managed BigQuery MCP (`mcp_bigquery_toolset`)
- **What it does**: Connects to Google's managed BigQuery MCP server (`https://bigquery.googleapis.com/mcp`) using `McpToolset` with an `OAuth2` scheme and `AuthCredential`.
- **What to observe**:
  1. When prompted, ADK pauses the turn and renders a **"Sign in"** button in the UI (Human-in-the-Loop OAuth2 flow).
  2. Once authenticated, `SELECT SESSION_USER()` returns **your personal Google email**.
  3. **Lifecycle behavior**: `McpToolset` relies on ADK's generic `CredentialManager` and `InMemoryCredentialService`, which caches the token **in the Python server's memory per `(app_name, user_id)`** (and strips `temp:` keys before persisting the session).
     - If you create a **new session** in the UI without restarting the server, you are **not** asked to sign in again.
     - If you **stop and restart `adk web`** and reload the existing session, the in-memory credential is gone and you **must re-authenticate**.

#### Scenario 3: End-User OAuth2 via Native `BigQueryToolset` (`oauth_bigquery_toolset`)
- **What it does**: Uses `BigQueryCredentialsConfig(client_id=..., client_secret=..., scopes=...)` with `BigQueryToolset`.
- **What to observe**:
  1. Like Scenario 2, ADK triggers the interactive OAuth2 consent flow and runs the query under your personal identity.
  2. **Lifecycle behavior**: Unlike `McpToolset`, `BigQueryToolset` uses `GoogleCredentialsManager`, which serializes the OAuth2 credentials (`token`, `refresh_token`, `client_secret`) directly into **`session.state["bigquery_token_cache"]`**.
     - If you create a **new session**, `session.state` is empty, so you **must re-authenticate**.
     - If you **stop and restart `adk web`** and reload your previous session from Vertex AI Agent Engine, the token is reloaded from `session.state` and **you remain authenticated without signing in again**!

#### Scenario 4: End-User OAuth2 via a Custom Tool (`custom_rest_bigquery_tool`)
- **What it does**: Shows how to build your own custom Python tool (`execute_sql_rest_api`) that makes a direct HTTP REST call (`POST https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries` with `Authorization: Bearer <token>`) while keeping the exact same clean wrapper architecture and session-state storage as `BigQueryToolset`:
  1. **Subclass `BaseGoogleCredentialsConfig`**: Defines a custom session state cache key (`self._token_cache_key = "custom_tool_token_cache"`) and default OAuth scopes in `__post_init__`.
  2. **Wrap the pure Python function in `GoogleTool`**: `GoogleTool(func=execute_sql_rest_api, credentials_config=custom_credentials_config)` hides the `credentials: Credentials` parameter from the LLM, orchestrates the OAuth2 flow, injects the valid `Credentials` object at runtime, persists tokens in `session.state["custom_tool_token_cache"]`, and supports Cloud KMS encryption.

---

## The Plot Twist: Plaintext Tokens in Persisted Sessions vs. Cloud KMS (`ADK 2.12+`)

When you persist sessions using a remote session service (such as **Vertex AI Agent Engine** via `--session_service_uri "agentengine://..."` or a SQL database), everything in `session.state` is saved to the backend store.

### Without `GOOGLE_CREDENTIAL_KMS_KEY` (Default Behavior)
If you inspect the persisted session in Agent Engine after running **Scenario 3** or **Scenario 4**:
```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${ENGINE_LOCATION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${ENGINE_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}/sessions/${SESSION_ID}" | jq .
```
You will see the user's OAuth2 `token`, `refresh_token`, and the app's `client_secret` stored **in plaintext**:
```json
{
  "sessionState": {
    "bigquery_token_cache": "{\"token\": \"ya29.a0AeX...\", \"refresh_token\": \"1//036V...\", \"client_secret\": \"GOCSPX-...\"}"
  }
}
```
Anyone with read access to the session store (or logs/backups) can hijack the user's OAuth2 session and refresh tokens indefinitely.

### With `GOOGLE_CREDENTIAL_KMS_KEY` (ADK 2.12+ Envelope Encryption)
Starting in **ADK 2.12**, setting the `GOOGLE_CREDENTIAL_KMS_KEY` environment variable (or passing `kms_key_name` to `BaseGoogleCredentialsConfig`) enables **envelope encryption**:
- ADK generates a local Fernet Data Encryption Key (DEK), encrypts `token`, `refresh_token`, and `client_secret` (`kms:gAAAAAB...`), wraps the DEK using **Google Cloud KMS** (`wrapped_dek`), and stores the encrypted payload in `session.state`:
```json
{
  "sessionState": {
    "bigquery_token_cache": "{\"token\": \"kms:gAAAAABqzoKENjFl...\", \"refresh_token\": \"kms:gAAAAABqzoKEUopv...\", \"client_secret\": \"kms:gAAAAABqzoKEpf3F...\", \"wrapped_dek\": \"CiUA7ZY7OlojKJ++...\", \"kms_key_name\": \"projects/.../cryptoKeys/test/cryptoKeyVersions/17\"}"
  }
}
```

---

## Prerequisites & Google Cloud Environment Setup

Before launching the demo, complete the following steps to configure your Google Cloud project.

### Step 1: Python Environment & Required GCP APIs

1. **Install Python dependencies** (requires **Python 3.11+**):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Enable the required Google Cloud APIs**:
   ```bash
   export PROJECT_ID="your-project-id"
   gcloud config set project "${PROJECT_ID}"

   gcloud services enable \
     bigquery.googleapis.com \
     aiplatform.googleapis.com \
     secretmanager.googleapis.com \
     cloudkms.googleapis.com
   ```

---

### Step 2: Configure the OAuth Consent Screen (Google Auth Platform)

To allow end users to authenticate via OAuth 2.0 (Scenarios 2, 3, and 4), your Google Cloud project must have a configured **OAuth Consent Screen**.

1. Open the **[Google Auth Platform (OAuth Consent Screen)](https://console.cloud.google.com/auth/overview)** in the Google Cloud Console.
2. **Branding / App Information**:
   - Enter an **App name** (e.g., `ADK BigQuery Security Demo`) and select a **User support email**.
3. **Audience**:
   - Choose **Internal** if you belong to a Google Workspace organization and only test with users inside your org.
   - Choose **External** if you use a personal `@gmail.com` account. In **Testing** publishing status, make sure to add your Google account email under **Test users** so Google allows you to complete the login flow.
4. **Data Access (Scopes)**:
   - Click **Add or Remove Scopes** and add the BigQuery scope:
     ```text
     https://www.googleapis.com/auth/bigquery
     ```
   - Save your changes.

> 📚 **Documentation**:
> - [Configure the OAuth consent screen & choose scopes (Google Cloud Docs)](https://developers.google.com/workspace/guides/configure-oauth-consent)
> - [ADK Authentication & OAuth2 Guide](https://google.github.io/adk-docs/tools/authentication/)

---

### Step 3: Create the OAuth 2.0 Client ID & Authorize Redirect URIs

Next, create the OAuth 2.0 Client credentials that `adk web` will use to initiate the authorization code flow.

1. In the Google Cloud Console, go to **[APIs & Services > Credentials](https://console.cloud.google.com/apis/credentials)** (or **[Google Auth Platform > Clients](https://console.cloud.google.com/auth/clients)**).
2. Click **+ Create Credentials > OAuth client ID**.
3. Select **Application type**: **Web application**.
4. Give it a name (e.g., `adk-web-client`).
5. Under **Authorized redirect URIs**, add the exact callback URL where `adk web` is hosted:

   - **If running `adk web` locally on your laptop**:
     ```text
     http://localhost:8000/dev-ui/
     http://127.0.0.1:8000/dev-ui/
     ```
   - **If running `adk web` inside Google Cloud Shell (Web Preview)**:
     In Cloud Shell, the browser accesses port `8000` through a persistent `*.cloudshell.dev` proxy domain stored in `$WEB_HOST`. Run this command in Cloud Shell to print your exact URI:
     ```bash
     echo "https://8000-${WEB_HOST}/dev-ui/"
     ```
     Then add that exact URL (e.g., `https://8000-cs-123456789-default.cs-europe-west1-xxxx.cloudshell.dev/dev-ui/`) to **Authorized redirect URIs**.

   > ⚠️ **Important**: Do **not** forget the trailing slash (`/`) after `/dev-ui/`! If it is missing, Google OAuth2 will return `Error 400: redirect_uri_mismatch`.

6. Click **Create** and copy the generated **Client ID** and **Client Secret**.
7. **(Recommended)** Store them in **Google Cloud Secret Manager** so they are never hardcoded on disk:
   ```bash
   printf "%s" "YOUR_OAUTH_CLIENT_ID" | gcloud secrets create adk-oauth-client-id --data-file=-
   printf "%s" "YOUR_OAUTH_CLIENT_SECRET" | gcloud secrets create adk-oauth-client-secret --data-file=-
   ```

> 📚 **Documentation**: [Create OAuth 2.0 access credentials (Google Cloud Docs)](https://developers.google.com/workspace/guides/create-credentials#oauth-client-id)

---

### Step 4: Create and Configure the Cloud KMS Key (Symmetric Encryption)

In **ADK 2.12+**, `GOOGLE_CREDENTIAL_KMS_KEY` uses **envelope encryption**:
- ADK generates a local **Fernet Data Encryption Key (DEK)** to encrypt the OAuth2 credentials (`token`, `refresh_token`, `client_secret`),
- ADK then calls **Google Cloud KMS** (`Encrypt` / `Decrypt` API) to wrap and unwrap that DEK.

#### What type of KMS key is required?
You need a **Symmetric encryption/decryption key** (`--purpose="encryption"`, algorithm `google-symmetric-encryption`).

#### Commands to create the KeyRing, CryptoKey, and grant IAM permissions:
```bash
export PROJECT_ID="your-project-id"
export KMS_LOCATION="europe-west1"   # or "global", "us-central1", etc.
export KEYRING_NAME="adk-security-keyring"
export KEY_NAME="adk-session-key"

# 1. Create the KeyRing
gcloud kms keyrings create "${KEYRING_NAME}" \
    --location="${KMS_LOCATION}"

# 2. Create a Symmetric Encryption key
gcloud kms keys create "${KEY_NAME}" \
    --location="${KMS_LOCATION}" \
    --keyring="${KEYRING_NAME}" \
    --purpose="encryption"

# 3. Grant the Encrypter/Decrypter role to the identity running ADK
#    (e.g., your user account or the demo Service Account)
gcloud kms keys add-iam-policy-binding "${KEY_NAME}" \
    --location="${KMS_LOCATION}" \
    --keyring="${KEYRING_NAME}" \
    --member="user:$(gcloud config get-value account)" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter"
```

#### Resource name format for `GOOGLE_CREDENTIAL_KMS_KEY`:
You can pass either the **CryptoKey** resource name (recommended) or a specific **CryptoKeyVersion**:
```text
projects/<PROJECT_ID>/locations/<KMS_LOCATION>/keyRings/<KEYRING_NAME>/cryptoKeys/<KEY_NAME>
```

> 📚 **Documentation**:
> - [Create symmetric encryption keys in Cloud KMS](https://cloud.google.com/kms/docs/create-key#create_a_symmetric_encryption_key)
> - [Cloud KMS Envelope Encryption Overview](https://cloud.google.com/kms/docs/envelope-encryption)

---

### Step 5: (Optional) Create a Vertex AI Agent Engine Instance for Session Persistence

By default, `adk web` stores sessions in local memory or SQLite. To demonstrate remote session persistence in **Vertex AI Agent Engine** (`--session_service_uri "agentengine://..."`) and inspect the stored session via REST API:

```python
import vertexai
from vertexai import agent_engines

vertexai.init(project="your-project-id", location="europe-west1")
# Create a bare Agent Engine instance to host persisted ADK sessions
remote_app = agent_engines.create()
print(remote_app.resource_name)
# Output: projects/<PROJECT_NUMBER>/locations/europe-west1/reasoningEngines/<REASONING_ENGINE_ID>
```

> 📚 **Documentation**: [Vertex AI Agent Engine Sessions Guide](https://google.github.io/adk-docs/sessions/session/)

---

## Running the Demo (`command.sh`)

All commands used during the live demo are available in [`command.sh`](command.sh).

### 1. Configure Variables
Edit the variables at the top of [`command.sh`](command.sh) to match your GCP project:
```bash
PROJECT_ID="your-project-id"
PROJECT_NUMBER="your-project-number"
GENAI_LOCATION="eu"
ENGINE_LOCATION="europe-west1"
REASONING_ENGINE_ID="your-reasoning-engine-id"
KMS_KEY="projects/${PROJECT_ID}/locations/${ENGINE_LOCATION}/keyRings/your-keyring/cryptoKeys/your-key"
```

### 2. Launch Option A — Without KMS Encryption (Plaintext Session State)
```bash
ADK_ALLOW_REMOTE_AGENT_BUILDER=1 \
OAUTH_CLIENT_ID="$(gcloud secrets versions access latest --secret=adk-oauth-client-id --project=${PROJECT_ID})" \
OAUTH_CLIENT_SECRET="$(gcloud secrets versions access latest --secret=adk-oauth-client-secret --project=${PROJECT_ID})" \
GOOGLE_GENAI_USE_VERTEXAI=TRUE \
GOOGLE_CLOUD_LOCATION="${GENAI_LOCATION}" \
GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
adk web --allow_origins "*" --session_service_uri "agentengine://projects/${PROJECT_NUMBER}/locations/${ENGINE_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}"
```
> **Note on `--allow_origins "*"` and `ADK_ALLOW_REMOTE_AGENT_BUILDER=1`**: In ADK 2.12+, `adk web` enforces strict DNS-rebinding (`Origin` / `Host`) and reverse-proxy (`X-Forwarded-For`) checks when bound to `127.0.0.1`. These two flags are required when accessing `adk web` through **Google Cloud Shell Web Preview** or an IDE reverse proxy.

### 3. Launch Option B — With Cloud KMS Encryption (`GOOGLE_CREDENTIAL_KMS_KEY`)
```bash
ADK_ALLOW_REMOTE_AGENT_BUILDER=1 \
GOOGLE_CREDENTIAL_KMS_KEY="${KMS_KEY}" \
OAUTH_CLIENT_ID="$(gcloud secrets versions access latest --secret=adk-oauth-client-id --project=${PROJECT_ID})" \
OAUTH_CLIENT_SECRET="$(gcloud secrets versions access latest --secret=adk-oauth-client-secret --project=${PROJECT_ID})" \
GOOGLE_GENAI_USE_VERTEXAI=TRUE \
GOOGLE_CLOUD_LOCATION="${GENAI_LOCATION}" \
GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
adk web --allow_origins "*" --session_service_uri "agentengine://projects/${PROJECT_NUMBER}/locations/${ENGINE_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}"
```

### 4. Inspect & Revoke Session Tokens
After authenticating in the ADK Web UI, copy the **Session ID** from the top bar and run the inspection snippet from [`command.sh`](command.sh):
```bash
SESSION_ID="<YOUR_SESSION_ID>"
SESSION_URL="https://${ENGINE_LOCATION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${ENGINE_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}/sessions/${SESSION_ID}"

# 1. Inspect the session state (plaintext vs. KMS-encrypted)
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     -H "Content-Type: application/json" \
     "${SESSION_URL}" | jq .

# 2. Immediately revoke an exposed plaintext token & delete the session
EXPOSED_TOKEN=$(curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" "${SESSION_URL}" \
  | jq -r '(.sessionState.bigquery_token_cache // .sessionState.custom_tool_token_cache) | fromjson | .token // empty')

curl -s -X POST "https://oauth2.googleapis.com/revoke?token=${EXPOSED_TOKEN}"
curl -s -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" "${SESSION_URL}"
```
