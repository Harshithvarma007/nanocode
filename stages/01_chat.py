"""Stage 1 - Talk to a model.

Every coding agent starts here: send a list of messages, get a reply back.
The model remembers nothing between calls - the `messages` list IS its memory.

    ollama pull qwen3:8b
    python stages/01_chat.py

Prefer a hosted model? Any OpenAI-compatible API works:
    BASE_URL=https://api.openai.com/v1 API_KEY=sk-... MODEL=gpt-5 python stages/01_chat.py
"""

import os

from openai import OpenAI

client = OpenAI(base_url=os.getenv("BASE_URL", "http://localhost:11434/v1"), api_key=os.getenv("API_KEY", "ollama"))
MODEL = os.getenv("MODEL", "qwen3:8b")

messages = [{"role": "system", "content": "You are a helpful coding assistant. Be concise."}]

while True:
    messages.append({"role": "user", "content": input("\n> ")})
    reply = client.chat.completions.create(model=MODEL, messages=messages).choices[0].message
    messages.append({"role": "assistant", "content": reply.content})
    print(reply.content)
