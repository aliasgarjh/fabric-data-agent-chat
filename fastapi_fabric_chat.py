"""
FastAPI Chat App Template with Azure AD User Login (MSAL) and Fabric Data Agent Integration

- Users authenticate via Azure AD (Authorization Code Flow)
- Each user's access token is used to create an AgentsClient for their chat session
- Uses MSAL for Python for authentication
- Requires configuration in Azure AD App Registration (redirect URI, API permissions, etc.)

Instructions:
1. Register an app in Azure AD (set redirect URI to http://localhost:8000/auth/redirect)
2. Grant API permissions for Microsoft Fabric and OpenID scopes
3. Fill in the config variables below
4. Install dependencies: fastapi, uvicorn, msal, jinja2, aiofiles, python-multipart, azure-ai-agents
5. Run: uvicorn fastapi_fabric_chat:app --reload
"""


import os
import time
import uuid
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from msal import ConfidentialClientApplication
from azure.ai.agents import AgentsClient
from semantic_kernel import Kernel
from semantic_kernel.functions import kernel_function, KernelArguments
from dotenv import load_dotenv

# Load .env before any config
load_dotenv()

# Redis setup with in-memory fallback for dev

# ------------------- TOKEN CREDENTIAL -------------------
class TokenCredential:
    def __init__(self, token):
        self.token = token
    def get_token(self, *args, **kwargs):
        class Token:
            def __init__(self, token):
                self._token = token
                self.expires_on = int(time.time()) + 3600  # 1 hour from now
            @property
            def token(self):
                return self._token
        return Token(self.token)

# ------------------- CONFIGURATION -------------------

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
TENANT_ID = os.getenv("TENANT_ID")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_PATH = "/auth/redirect"
REDIRECT_URI = f"http://localhost:8000{REDIRECT_PATH}"
SCOPE = ["https://ai.azure.com/.default"]
FABRIC_PROJECT_ENDPOINT = os.getenv("FABRIC_PROJECT_ENDPOINT")
FABRIC_AGENT_ID = os.getenv("FABRIC_AGENT_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET") or str(uuid.uuid4())

# Warn if critical config is missing
missing = []
for var in ["CLIENT_ID", "CLIENT_SECRET", "TENANT_ID", "FABRIC_PROJECT_ENDPOINT", "FABRIC_AGENT_ID"]:
    if not os.getenv(var):
        missing.append(var)
if missing:
    import warnings
    warnings.warn(f"Missing required environment variables: {', '.join(missing)}. Check your .env file.")

# ------------------- FASTAPI APP SETUP -------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="static")

# ------------------- MSAL CONFIDENTIAL CLIENT -------------------
msal_app = ConfidentialClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    client_credential=CLIENT_SECRET
)

# ------------------- SEMANTIC KERNEL PLUGIN -------------------

class DemoFabricDataPlugin:
    def __init__(self, endpoint: str, agent_id: str, credential, thread=None):
        self.endpoint = endpoint
        self.agent_id = agent_id
        self.credential = credential
        self.thread = thread

    @kernel_function(
        description="Query a Microsoft Fabric data source using natural language.",
        name="query_fabric_data"
    )
    async def query_fabric_data(self, query: str) -> str:
        try:
            agents_client = AgentsClient(endpoint=self.endpoint, credential=self.credential)
            with agents_client:
                if self.thread is None:
                    self.thread = agents_client.threads.create()
                agents_client.messages.create(thread_id=self.thread.id, role="user", content=query)
                run = agents_client.runs.create(thread_id=self.thread.id, agent_id=self.agent_id)
                import time
                timeout_seconds = 30
                poll_interval = 1
                waited = 0
                while True:
                    run_status = agents_client.runs.get(thread_id=self.thread.id, run_id=run.id)
                    if run_status.status in ("completed", "failed", "cancelled"):
                        break
                    time.sleep(poll_interval)
                    waited += poll_interval
                    if waited >= timeout_seconds:
                        return {
                            "timeout": True,
                            "thread_id": self.thread.id,
                            "run_id": run.id,
                            "message": f"Error: Agent run did not complete within {timeout_seconds} seconds. Last status: {run_status.status}"
                        }
                messages = list(agents_client.messages.list(thread_id=self.thread.id))
                for msg in messages:
                    if msg.role == "assistant":
                        if hasattr(msg, 'text_messages') and msg.text_messages:
                            return msg.text_messages[-1].text['value']
                        if hasattr(msg, 'content'):
                            return msg.content
                        return str(msg)
                return "No response received."
        except Exception as e:
            return f"Error querying Fabric data: {str(e)}"

# ------------------- AUTH HELPERS -------------------
def get_token_from_session(request: Request):
    return request.session.get("access_token")

def get_user_from_session(request: Request):
    return request.session.get("user")

# ------------------- ROUTES -------------------
@app.get("/")
def home(request: Request):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("chat.html", {"request": request, "user": user})

@app.get("/login")
def login(request: Request):
    state = str(uuid.uuid4())
    request.session["state"] = state
    auth_url = msal_app.get_authorization_request_url(
        SCOPE,
        state=state,
        redirect_uri=REDIRECT_URI
    )                          
    return RedirectResponse(auth_url)

@app.get(REDIRECT_PATH)
def authorized(request: Request):
    if request.query_params.get("state") != request.session.get("state"):
        return HTMLResponse("State mismatch. Please try again.")
    code = request.query_params.get("code")
    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=["https://ai.azure.com/.default"],
        redirect_uri=REDIRECT_URI
    )
    if "access_token" in result:
        request.session["access_token"] = result["access_token"]
        request.session["user"] = result.get("id_token_claims", {}).get("name", "User")
        return RedirectResponse("/")
    return HTMLResponse(f"Login failed: {result}")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


from fastapi.responses import JSONResponse



import hashlib
def get_user_id(request: Request):
    user = get_user_from_session(request)
    if not user:
        return None
    # Use a hash of the username for privacy
    return hashlib.sha256(user.encode()).hexdigest()

@app.post("/chat")
async def chat(request: Request):
    access_token = get_token_from_session(request)
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    message = data.get("message")
    if not message:
        return JSONResponse({"error": "No message provided"}, status_code=422)

    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"error": "User not found in session"}, status_code=401)




    credential = TokenCredential(access_token)
    kernel = Kernel()
    plugin = DemoFabricDataPlugin(FABRIC_PROJECT_ENDPOINT, FABRIC_AGENT_ID, credential)
    kernel.add_plugin(plugin, plugin_name="fabric_data_plugin")
    plugin_func = kernel.plugins["fabric_data_plugin"].functions["query_fabric_data"]
    args = KernelArguments(query=message)
    result = await plugin_func.invoke(kernel=kernel, arguments=args)


    return JSONResponse({"response": str(result)})
