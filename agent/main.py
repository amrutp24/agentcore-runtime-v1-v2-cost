"""Minimal Strands agent for AgentCore Runtime.

One trivial tool, tiny prompts, low max tokens. Stateless per invocation so
token counts stay flat even in a long session (otherwise history growth would
confound the cost comparison).
"""
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

# Default verified against the Bedrock model card (in-Region in us-east-1,
# client-side tool calling supported).
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-micro-v1:0")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "128"))
# TODO(verify): whether the runtime sets AWS_REGION itself; BEDROCK_REGION is
# set by infra/ so we do not depend on it.
REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
DEFAULT_PROMPT = "What is 17 + 25? Use the add tool. Answer in one short sentence."


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


# Built at import time so a V2 snapshot captures an initialised agent.
model = BedrockModel(
    model_id=MODEL_ID,
    max_tokens=MAX_TOKENS,
    temperature=0.0,
    region_name=REGION,
)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    prompt = (payload or {}).get("prompt") or DEFAULT_PROMPT
    agent = Agent(model=model, tools=[add], system_prompt="Be brief.", callback_handler=None)
    result = agent(prompt)
    # Usage keys (inputTokens/outputTokens) confirmed in the installed SDK; TODO(verify): metrics.accumulated_usage path on a live run.
    usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None) or {}
    return {
        "result": str(result),
        "usage": {
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
        },
        "variant": os.environ.get("VARIANT_ID"),
    }


if __name__ == "__main__":
    app.run()
