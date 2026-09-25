### How to start the agent?

After configuring the environment variables, choose the native Gemini Live profile (the default) or the preserved Groq/Silero/Cartesia cascade in `agent/.env`:

```dotenv
LUMINE_PIPELINE=gemini_live
# LUMINE_PIPELINE=legacy_cascade
```

Then run:

```text
python agent.py dev
```