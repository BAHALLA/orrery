# AEP-013: Security Hardening & Authentication Layer

| Field | Value |
|-------|-------|
| **Status** | proposed |
| **Priority** | P3 |
| **Effort** | High (7-10 days) |
| **Impact** | Critical |
| **Dependencies** | AEP-011 (deployment hardening) |

## Gap Analysis

### Current Implementation
The project has solid security foundations:
- **RBAC**: 3-role hierarchy (viewer/operator/admin) enforced via `GuardrailsPlugin`
- **Input validation**: 5 validators with safety constants preventing injection attacks
- **Guardrails**: `@destructive` and `@confirm` decorators for dangerous operations
- **Audit logging**: Structured JSON with secret redaction
- **Authentication enforcement**: `set_user_role()` marks server-trusted roles; `ensure_default_role()` forces `viewer` for unset roles

However, there is **no authentication layer** — the system trusts the integration layer
(Slack bot, web UI) to set the user role correctly.

### What ADK Provides
ADK's safety documentation recommends a multi-layered approach:

1. **Agent-Auth**: Service account identity for tool calls to external systems
2. **User-Auth**: OAuth-based identity delegation (agent acts as the user)
3. **In-tool guardrails**: Policy enforcement via `ToolContext` with developer-set constraints
4. **Gemini safety filters**: Configurable content safety thresholds
5. **Callbacks/Plugins for guardrails**: Pre-validation of model and tool I/O
6. **Gemini as a Judge**: LLM-based safety screening of inputs/outputs
7. **PII Redaction Plugin**: Before-tool callback to redact PII

### Gap
1. **No authentication**: Anyone with network access can send requests
2. **No OAuth/JWT verification**: User identity is self-declared
3. **No API key management**: LLM keys in environment variables
4. **No PII redaction**: Infrastructure data (IPs, hostnames) may leak
5. **No content safety filters**: No screening for prompt injection attempts
6. **No network isolation**: No guidance for VPC/firewall configuration

## Proposed Solution

### Step 1: JWT Authentication Middleware

```python
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer
import jwt

security = HTTPBearer()

async def verify_token(credentials = Depends(security)):
    try:
        payload = jwt.decode(
            credentials.credentials,
            os.getenv("JWT_SECRET"),
            algorithms=["HS256"],
        )
        return payload
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Map JWT claims to RBAC roles
def extract_role(token_payload: dict) -> str:
    roles = token_payload.get("roles", [])
    if "admin" in roles:
        return "admin"
    elif "operator" in roles:
        return "operator"
    return "viewer"
```

### Step 2: Auth Plugin

```python
class AuthPlugin(BasePlugin):
    """Validates authentication and sets user role from JWT claims."""

    def __init__(self):
        super().__init__(name="auth")

    async def on_user_message_callback(self, *, invocation_context, user_message):
        # Extract auth context from session state (set by HTTP middleware)
        auth_context = invocation_context.session.state.get("_auth")
        if not auth_context:
            return types.Content(
                parts=[types.Part(text="Authentication required.")],
                role="model",
            )

        # Set verified role
        set_user_role(invocation_context.session, auth_context["role"])
        return None
```

### Step 3: PII Redaction Plugin

Create a plugin that redacts infrastructure PII before tool output reaches the LLM:

```python
class PIIRedactionPlugin(BasePlugin):
    """Redacts infrastructure-sensitive data from tool outputs."""

    PATTERNS = [
        (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '[REDACTED_IP]'),
        (r'password["\s:=]+\S+', 'password=[REDACTED]'),
        (r'token["\s:=]+\S+', 'token=[REDACTED]'),
        (r'(?i)api[_-]?key["\s:=]+\S+', 'api_key=[REDACTED]'),
    ]

    def __init__(self):
        super().__init__(name="pii_redaction")

    async def after_tool_callback(self, *, tool, args, tool_context, result):
        if isinstance(result, dict):
            result = self._redact_dict(result)
        return result

    def _redact_dict(self, data):
        """Recursively redact sensitive patterns in dict values."""
        ...
```

### Step 4: Prompt Injection Detection

Add a safety screening plugin (inspired by ADK's "Gemini as a Judge" pattern):

```python
class SafetyScreenPlugin(BasePlugin):
    """Screens user inputs for prompt injection attempts."""

    INJECTION_PATTERNS = [
        r"ignore previous instructions",
        r"forget your instructions",
        r"you are now",
        r"system prompt",
        r"reveal your",
    ]

    async def on_user_message_callback(self, *, invocation_context, user_message):
        text = " ".join(p.text for p in user_message.parts if p.text)
        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                logger.warning("Potential prompt injection detected", extra={"input": text[:200]})
                return types.Content(
                    parts=[types.Part(text="I can only help with DevOps tasks.")],
                    role="model",
                )
        return None
```

### Step 5: Gemini Safety Filters

For Gemini models, enable content safety filters:

```python
from google.genai import types

agent = create_agent(
    name="devops_assistant",
    generate_content_config=types.GenerateContentConfig(
        safety_settings=[
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
            ),
        ],
    ),
)
```

### Step 6: Secrets Management

Replace environment variables with a secrets manager:

```python
# core/ai_agents_core/secrets.py
class SecretsManager:
    """Pluggable secrets management."""

    @staticmethod
    def get(key: str) -> str:
        # Try vault first, fall back to env vars
        if vault_client := _get_vault_client():
            return vault_client.read(f"secret/data/agents/{key}")
        return os.getenv(key, "")
```

## Affected Files

| File | Change |
|------|--------|
| `core/ai_agents_core/auth.py` | New: JWT verification, AuthPlugin |
| `core/ai_agents_core/pii.py` | New: PIIRedactionPlugin |
| `core/ai_agents_core/safety.py` | New: SafetyScreenPlugin |
| `core/ai_agents_core/secrets.py` | New: SecretsManager |
| `core/ai_agents_core/plugins.py` | Add auth/PII/safety plugins to `default_plugins()` |
| `agents/slack-bot/slack_bot/app.py` | Add Slack signature verification |
| `core/tests/test_auth.py` | New: auth tests |
| `core/tests/test_pii.py` | New: PII redaction tests |
| `core/tests/test_safety.py` | New: prompt injection detection tests |

## Acceptance Criteria

- [ ] JWT authentication required for HTTP endpoints
- [ ] User role derived from verified JWT claims (not self-declared)
- [ ] PII redaction applied to all tool outputs
- [ ] Prompt injection patterns detected and blocked
- [ ] Gemini safety filters enabled for content screening
- [ ] Secrets manager with vault integration (env var fallback)
- [ ] Slack bot verifies request signatures
- [ ] All security features have comprehensive tests
- [ ] Security documentation with threat model

## Notes

- Authentication is the most critical gap. Without it, RBAC is meaningless since anyone can claim to be an admin.
- Start with JWT + RBAC mapping. OAuth2 with a proper identity provider (Auth0, Keycloak, Google IAP) is the production target.
- The PII redaction plugin should be configurable — some users may need to see IPs and hostnames for debugging.
- Prompt injection detection via regex is a baseline. For production, consider the "Gemini as a Judge" pattern using a fast, cheap model (Gemini Flash Lite) to screen inputs.
