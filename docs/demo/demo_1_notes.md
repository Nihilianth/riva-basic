Dynamic audio threshold based on user speech


I'd like to draft a plan for a Demo application:
- It will be presented in a controlled setting. Audience are German people with mental disabilities aged 14-35, one will be called to the front to check it out. This means that:
    - Interface must be clean, with large buttons (Also one to start the VAD, since checkboxes are small)
    - Clear indication that the system is listening, thinking (LLM) or speaking (ASR) right now, or preparing to
        - This could be done with large emojis for example
    - Debug output and something irrelevant for the target audience should be hidden behind (expandable) spoilers.
    - Complete user interface and notifications must be in German
    - Audio, transcripts and LLM responses must be logged with timestamps. At later point (perhaps outside this epic), there will be some kind of player to review the captures.
    - Users have to be able to select a voice from the available ones (presented as dropdown, with short demo when a voice is selected), the demo should/can be cached.
    - For the presenter, at least 3 System prompts for the LLM (Persona prompt part), which can be selected with a dropdown. Perhaps something like Expert, an older person or a buddy.
    - For the presenter, a dropdown with a model selection for the LLM. 
- The actual task (which is a new architectural shift here) is as follows:
    - The system has to not only receive audio and forward to LLM
    - It must mimic a journaling tool. The user speaks something (not always coherently). The LLM that receives the transcript must:
        a) Generate a written version of the passage spoken by user. It should correct the test so it makes sense.
        b) Generate an answer for the TTS (It has to just support the journaling task, so perhaps asking leading questions so the user provides more information about his day or so). Not just something like "Ah you want to make an entry in your journal."
        c) answer in German ONLY!
        d) Ideally provide another chunk of the answer
        e) Educational aspect of the LLM -> If there is a word that fits the description of what the user told, that he might not know, the LLM could ask the user as part of the ASR answer (for example "Ah, kennst du eigentlich das Word XY, das beschreibt das eigentlich ganz gut").
    - the transcript must be saved verbatim (for example the LLM generated version of the text and the transcript from ASR could be shown in text frames (like quill) as tabs, with the LLM generated version being the front facing one)
    - I am not sure of the exact architecture that should be used here, since this is more an agentic approach (or maybe not as we do not necssasrily need planning, tool calling, or autonomous decision-making for this demo).
    - We have the basic VAD/ASR/LLM/TTS stack here which is iirc modular, so it should speed up the development of this demo I think.
    - The demo should get it's own folder somewhere in this project, so setup instructions and perhaps the session outputs are logged there. Each time the page is reloaded I think should be considered as its own session (Accounts will come later)


So in short, the LLM's responsibilities here are:
- Correct the user's spoken text.
- Preserve what the user actually meant.
- Respond conversationally.
- Ask follow-up questions.
- Introduce educational vocabulary.
- LLM must never invent events or change the meaning of what the user said.
- When the intended meaning is uncertain, the assistant should ask for clarification rather than silently guess.
I believe that the agent must produce structured output which the demo app will then parse.

Imagine the following conversation:
User: „Heute war ich im Zoo.“
Assistant: „Was hast du dort gesehen?“
User: „Elefanten und einen ganz großen Löwen.“
this should produce one continuously updated entry: journal should be a cumulative document

Possible acceptance criteria:
- All visible participant-facing text is German.
- Every interaction produces a verbatim transcript and a separate journal representation.
- Journal text preserves the user's intended meaning.
- The assistant asks useful follow-up questions without repeating itself.
- Switching personas affects conversational style without changing core journaling behavior.
- Every processing stage displays its status.
- The application survives common ASR/LLM/TTS errors.
- Logs can be associated with a specific session and interaction.
- A complete demo can be operated without exposing technical controls to the participant.

- There are probably some Gradio features that I have no idea about. You could suggest which ones we could use, for example UI/UX related ones or purely functional.


- You should propose how we structure this architecture. You may ask me questions is something is unknown or might come up later in development of this epic. Do not generate code. Assess this first and we will discuss

**Additional instructions for the assessment phase**

Before proposing any implementation, please:

1. Inspect the existing experimental architecture and identify which components can be reused without modification.
2. Identify missing requirements, architectural uncertainties, and potential technical risks.
3. Compare possible architectural approaches, particularly a stateful LLM workflow versus a more agentic implementation. Recommend the simplest approach that meets the requirements.
4. Propose how session state, conversation history, journal generation, structured LLM output, and logging should interact.
5. Investigate relevant Gradio capabilities and distinguish between built-in functionality and features requiring custom implementation.
6. Recommend a practical separation between the participant-facing interface and presenter/debug controls.
7. Propose a phased implementation plan, distinguishing essential demo features from optional enhancements.
8. Define acceptance criteria and a practical testing strategy, including scenarios with imperfect German speech recognition.
9. Identify decisions that require my input before implementation.

**Important:** Do not generate code, modify files, or begin implementing anything yet. First present your architectural assessment, explain important trade-offs, and ask questions where requirements are genuinely ambiguous. Avoid introducing unnecessary complexity or frameworks.