"""Control agent for AgentCore Runtime: bare echo, nothing else.

No Strands, no boto3 client, no model call, no background threads. Used to tell
"the platform does not reclaim idle memory" apart from "the agent stack keeps the
session busy". Response shape matches agent/main.py so load/run_load.py logs it as is.
"""
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    return {
        "result": (payload or {}).get("prompt"),
        "usage": {"input_tokens": None, "output_tokens": None},
        "variant": os.environ.get("VARIANT_ID"),
    }


if __name__ == "__main__":
    app.run()
