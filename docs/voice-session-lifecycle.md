```text
User
 │
 │ Start
 ▼
React VoiceManager
 │
 ▼
Create unique room (lumine-uuid)
 │
 ▼
Request LiveKit token
 │
 ▼
Connect room
 │
 ▼
Dispatch agent = lumine
 │
 ▼
Python AgentServer
 │
 ▼
Agent job starts
 │
 ▼
AgentSession + VAD/STT/LLM/TTS
 │
 ▼
Conversation
 │
 │ Stop
 ▼
VoiceManager.stop()
 │
 ▼
Detach active session reference
 │
 ▼
End LiveKit session
 │
 ▼
Remove listeners
 │
 ▼
Release mic + audio tracks
 │
 ▼
Delete room
 │
 ▼
Agent job ends
 │
 ▼
AgentServer waits for next job
 │
 │ Start again
 ▼
New unique room / new session / new job
```