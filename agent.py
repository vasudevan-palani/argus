from google.adk.agents.llm_agent import Agent
import os
from google.adk.models.lite_llm import LiteLlm

# azure/gpt-4.1
# plan to move to azure/gpt-5.2
MODEL_NAME = os.getenv('MODEL_NAME')

from google.adk.tools.mcp_tool import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

cloudwatch_mcp = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uvx",
            args=["awslabs.cloudwatch-mcp-server@latest"],
            env={
                "AWS_PROFILE": os.getenv("AWS_PROFILE", "default"),
                "AWS_REGION": os.getenv("AWS_REGION", "us-east-1"),
                "FASTMCP_LOG_LEVEL": "ERROR",
            },
        ),
        timeout=60.0,
    ),
    # Optional: restrict which MCP tools your agent can use
    # tool_filter=["list_log_groups", "query_logs_insights", "get_metric_data"]
)

from datetime import datetime

def get_current_date() -> str:
    """
    Returns the current date in YYYY-MM-DD format.
    """
    return datetime.now().strftime("%Y-%m-%d")

root_agent = Agent(
    model = LiteLlm(model=MODEL_NAME),
    name = "Argus",
    tools=[get_current_date,cloudwatch_mcp],
    description = "A helpful assistant to supervise and assist with software applications.",
    instruction="You are a helpful assistant, answer questions to the best you can. Please use the tool to get the current date"
)