import httpx
import time
import json
import os
from fastapi import Request
from claude_to_chatgpt.util import num_tokens_from_string
from claude_to_chatgpt.logger import logger
from claude_to_chatgpt.models import model_map

role_map = {
    "system": "Human",
    "user": "Human",
    "assistant": "Assistant",
}

stop_reason_map = {
    "stop_sequence": "stop",
    "max_tokens": "length",
}


class ClaudeAdapter:
    def __init__(self, claude_base_url="https://api.anthropic.com"):
        self.claude_api_key = os.getenv("CLAUDE_API_KEY", None)
        self.claude_base_url = claude_base_url

    def get_api_key(self, headers):
        auth_header = headers.get("authorization", None)
        if auth_header:
            return auth_header.split(" ")[1]
        else:
            return self.claude_api_key

    def convert_messages_to_prompt(self, messages):
        prompt = ""
        for message in messages:
            role = message["role"]
            content = message["content"]
            transformed_role = role_map[role]
            prompt += f"\n\n{transformed_role.capitalize()}: {content}"
        prompt += "\n\nAssistant: "
        return prompt

    def openai_to_claude_params(self, openai_params):
        model = model_map.get(openai_params["model"], openai_params["model"])
        messages = openai_params["messages"]

        prompt = self.convert_messages_to_prompt(messages)

        claude_params = {
            "model": model,
            "prompt": prompt,
            "max_tokens_to_sample": 100000,
        }

        if openai_params.get("max_tokens"):
            claude_params["max_tokens_to_sample"] = openai_params["max_tokens"]

        if openai_params.get("stop"):
            claude_params["stop_sequences"] = openai_params.get("stop")

        if openai_params.get("temperature"):
            claude_params["temperature"] = openai_params.get("temperature")

        if openai_params.get("stream"):
            claude_params["stream"] = True

        return claude_params

    def claude_to_chatgpt_response_stream(self, claude_response):
        completion = claude_response.get("completion", "")
        completion_tokens = num_tokens_from_string(completion)
        openai_response = {
            "id": f"chatcmpl-{str(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "gpt-3.5-turbo-0613",
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": completion_tokens,
                "total_tokens": completion_tokens,
            },
            "choices": [
                {
                    "delta": {
                        "role": "assistant",
                        "content": completion,
                    },
                    "index": 0,
                    "finish_reason": stop_reason_map[claude_response.get("stop_reason")]
                    if claude_response.get("stop_reason")
                    else None,
                }
            ],
        }

        return openai_response

    def claude_to_chatgpt_response(self, claude_response):
        completion_tokens = num_tokens_from_string(
            claude_response.get("completion", "")
        )
        openai_response = {
            "id": f"chatcmpl-{str(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-3.5-turbo-0613",
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": completion_tokens,
                "total_tokens": completion_tokens,
            },
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": claude_response.get("completion", ""),
                    },
                    "index": 0,
                    "finish_reason": stop_reason_map[claude_response.get("stop_reason")]
                    if claude_response.get("stop_reason")
                    else None,
                }
            ],
        }

        return openai_response

    async def chat(self, request: Request):
        openai_params = await request.json()
        headers = request.headers
        claude_params = self.openai_to_claude_params(openai_params)
        api_key = self.get_api_key(headers)

        async with httpx.AsyncClient(timeout=120.0) as client:
            if not claude_params.get("stream", False):
                response = await client.post(
                    f"{self.claude_base_url}/v1/complete",
                    headers={
                        "x-api-key": api_key,
                        "accept": "application/json",
                        "content-type": "application/json",
                        "anthropic-version": "2023-06-01",
                    },
                    json=claude_params,
                )
                if response.is_error:
                    raise Exception(f"Error: {response.status_code}")
                claude_response = response.json()
                openai_response = self.claude_to_chatgpt_response(claude_response)
                yield openai_response
            else:
                async with client.stream(
                    "POST",
                    f"{self.claude_base_url}/v1/complete",
                    headers={
                        "x-api-key": api_key,
                        "accept": "application/json",
                        "content-type": "application/json",
                        "anthropic-version": "2023-06-01",
                    },
                    json=claude_params,
                ) as response:
                    if response.is_error:
                        raise Exception(f"Error: {response.status_code}")
                    async for line in response.aiter_lines():
                        if line:
                            stripped_line = line.lstrip("data:")
                            if stripped_line:
                                try:
                                    decoded_line = json.loads(stripped_line)
                                    stop_reason = decoded_line.get("stop_reason")
                                    if stop_reason:
                                        yield self.claude_to_chatgpt_response_stream(
                                            {
                                                "completion": "",
                                                "stop_reason": stop_reason,
                                            }
                                        )
                                        yield "[DONE]"
                                    else:
                                        completion = decoded_line.get("completion")
                                        if completion:
                                            openai_response = (
                                                self.claude_to_chatgpt_response_stream(
                                                    decoded_line
                                                )
                                            )
                                            yield openai_response
                                except json.JSONDecodeError as e:
                                    logger.debug(
                                        f"Error decoding JSON: {e}"
                                    )  # Debug output
                                    logger.debug(
                                        f"Failed to decode line: {stripped_line}"
                                    )  # Debug output
