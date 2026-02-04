from google.adk.agents.llm_agent import Agent
import os
from google.adk.models.lite_llm import LiteLlm

MODEL_NAME = os.getenv('MODEL_NAME')

root_agent = Agent(
    model = LiteLlm(model=MODEL_NAME),
    name = "Argus",
    description = "A helpful assistant to supervise and assist with software applications.",
    instruction="You are a helpful assistant, answer questions to the best you can."
)