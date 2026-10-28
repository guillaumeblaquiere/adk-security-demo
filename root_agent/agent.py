import os
import google.auth
from google.auth.credentials import Credentials
import requests
from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.agents.llm_agent import Agent
from google.adk.auth import OAuth2Auth
from google.adk.auth.auth_credential import AuthCredential, AuthCredentialTypes
from google.adk.tools._google_credentials import BaseGoogleCredentialsConfig
from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.tools.google_tool import GoogleTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

# Common configuration & OAuth2 credentials (injected at runtime via command.sh)
DEFAULT_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "gdglyon-cloudrun")
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "unset")
OAUTH_CLIENT_SECRET = os.getenv("OAUTH_CLIENT_SECRET", "unset")
BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/bigquery"]

# ============================================================================
# SCENARIO 1: Authenticate using Runtime Service Account / ADC
# - All users share the agent's technical identity (Service Account)
# - No per-user audit trail in BigQuery (SESSION_USER() returns the SA email)
# ============================================================================
application_default_credentials, _ = google.auth.default()

adc_credentials_config = BigQueryCredentialsConfig(
    credentials=application_default_credentials
)

adc_bigquery_toolset = BigQueryToolset(
    credentials_config=adc_credentials_config,
    tool_filter=["execute_sql"],
)

# ============================================================================
# SCENARIO 2: Authenticate via OAuth2 using Managed BigQuery MCP Server
# - End-user OAuth2 flow (SESSION_USER() returns the human user's email)
# - Credentials are kept in server RAM (InMemoryCredentialService) by user_id,
#   NOT persisted in session.state:
#     * New session (same user) -> still authenticated
#     * Server restart          -> connection lost, must re-authenticate
# ============================================================================
auth_scheme = OAuth2(
    flows=OAuthFlows(
        authorizationCode=OAuthFlowAuthorizationCode(
            authorizationUrl="https://accounts.google.com/o/oauth2/auth",
            tokenUrl="https://oauth2.googleapis.com/token",
            scopes={
                "https://www.googleapis.com/auth/bigquery": "Full access to BigQuery resources"
            },
        )
    )
)

auth_credential = AuthCredential(
    auth_type=AuthCredentialTypes.OAUTH2,
    oauth2=OAuth2Auth(
        client_id=OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
    ),
)

mcp_bigquery_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url="https://bigquery.googleapis.com/mcp"
    ),
    auth_credential=auth_credential,
    auth_scheme=auth_scheme,
    tool_filter=["execute_sql"],
)

# ============================================================================
# SCENARIO 3: Authenticate as the End User via OAuth2 (Native BigQueryToolset)
# - End-user OAuth2 flow (SESSION_USER() returns the human user's email)
# - Credentials are stored in session.state["bigquery_token_cache"]:
#     * New session             -> must re-authenticate
#     * Server restart + reload -> still authenticated!
# - Supports Cloud KMS envelope encryption via GOOGLE_CREDENTIAL_KMS_KEY (ADK 2.12+)
# ============================================================================
oauth_credentials_config = BigQueryCredentialsConfig(
    client_id=OAUTH_CLIENT_ID,
    client_secret=OAUTH_CLIENT_SECRET,
    scopes=BIGQUERY_SCOPES,
)

oauth_bigquery_toolset = BigQueryToolset(
    credentials_config=oauth_credentials_config,
    tool_filter=["execute_sql"],
)

# ============================================================================
# SCENARIO 4: Authenticate via OAuth2 using a Custom Tool wrapped in GoogleTool
# - Subclasses BaseGoogleCredentialsConfig (same pattern as ADK's built-in toolsets)
#   to define a custom session state key ("custom_tool_token_cache") and scopes
# - Pure business function wrapped in GoogleTool: automatic OAuth2 flow,
#   session state persistence, token refresh, and GOOGLE_CREDENTIAL_KMS_KEY encryption
# ============================================================================
class CustomCredentialsConfig(BaseGoogleCredentialsConfig):
    """Custom Google Credentials Configuration with its own session cache key and default scopes."""

    def __post_init__(self) -> "CustomCredentialsConfig":
        super().__post_init__()
        if not self.scopes:
            self.scopes = BIGQUERY_SCOPES
        self._token_cache_key = "custom_tool_token_cache"
        return self


custom_credentials_config = CustomCredentialsConfig(
    client_id=OAUTH_CLIENT_ID,
    client_secret=OAUTH_CLIENT_SECRET,
    scopes=BIGQUERY_SCOPES,
)


def execute_sql_rest_api(
    query: str,
    project_id: str = DEFAULT_PROJECT_ID,
    credentials: Credentials | None = None,
) -> dict:
    """Executes a SQL query in BigQuery via a direct HTTP REST API call."""
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "useLegacySql": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


custom_rest_bigquery_tool = GoogleTool(
    func=execute_sql_rest_api,
    credentials_config=custom_credentials_config,
)

# ============================================================================
# Initialize Root Agent (Uncomment ONLY the toolset for the current demo step)
# ============================================================================
root_agent = Agent(
    model="gemini-3.5-flash-lite",
    name="root_agent",
    description="Demo agent that executes SQL queries in BigQuery.",
    instruction=(
        "Use the provided BigQuery toolset to execute SQL queries. "
        f"Use the project '{DEFAULT_PROJECT_ID}' by default. "
        "If no specific query is provided, run 'SELECT SESSION_USER() AS current_user, 1 AS test'."
    ),
    tools=[
        adc_bigquery_toolset,          # SCENARIO 1: Runtime Service Account (ADC)
        # mcp_bigquery_toolset,        # SCENARIO 2: End-User OAuth2 (Managed BigQuery MCP)
        # oauth_bigquery_toolset,      # SCENARIO 3: End-User OAuth2 (Native BigQueryToolset)
        # custom_rest_bigquery_tool,   # SCENARIO 4: End-User OAuth2 (Custom Tool + Direct REST API)
    ],
)
