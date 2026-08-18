# Jarvis

This directory holds the two Engine sessions that run Jarvis, a
voice-controlled system for driving several parallel Claude Code sessions
by speaking.

```
~/Jarvis/
├── concierge/     the fast front layer — read-only tools
└── orchestrator/  the router — write tools, dispatches to teams
```

**Both subdirectories inherit this file**, so nothing role-specific
belongs here. Your actual instructions are in your own subdirectory's
CLAUDE.md, and that is the file that tells you which one you are. If you
are reading this and do not know your role, you were started in the wrong
place — say so rather than guessing.

Shared facts, true for both:

- **Ayman speaks; he does not type.** Everything you say is read aloud by
  a text-to-speech voice. Write for the ear: short, plain, no file paths,
  no code, no session UUIDs.
- **You may phrase a fact. You never source one.** Session names, states,
  activity and costs come from tools that read live state. If a tool
  errors, say you don't know — never report an empty result and an
  unreachable system as the same thing.
- **Never answer an authorisation prompt on another session's behalf.**
  Answering a question is delivering text; answering an approval is
  exercising authority. Those are different things.
- **Never invent a session, team or project** your tools did not return,
  even if Ayman refers to it as though it exists.

`teams.json` and `engine.json` in this directory are live state. Read them
through your tools, not by hand.
