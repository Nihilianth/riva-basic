# Trigger-eval queries for the debug-analysis description (adapted from python_bindings; the skill-creator pattern).
# Replay after every description change: python3 .claude/skills/check_skill_triggers.py
# Format: "+ kw1|kw2 :: query" should trigger; "- marker :: query" must NOT and the
# description must keep the boundary hint <marker> (the alternative tool).

+ debug|analyze :: analyze this system prompt for errors
+ audit|review :: audit the instruction block I pasted for contradictions
+ system prompt|instruction text :: check the system prompt I wrote for my agent
+ debug|audit :: debug or audit this prompt
- audio/vad pipeline :: debug why the VAD timeline stops at asr.session_ready
- doc-maintenance :: fix the routing table in AGENTS.md
