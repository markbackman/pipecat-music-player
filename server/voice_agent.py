"""Voice agent: pure conversation layer that delegates all UI work.

Exposes one tool, ``handle_request``. The tool forwards the user's query
verbatim to the UI agent and awaits the response. The UI agent chooses
the right screen action; the voice agent speaks the result.
"""

import os

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.services.openai.base_llm import OpenAILLMSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat_subagents.agents import LLMAgent, TaskError, tool
from pipecat_subagents.bus import AgentBus

SYSTEM_PROMPT = """\
You are the voice layer for a music player backed by a live music \
catalog. A separate UI layer owns all screen state. You do not know \
what is on screen. You do not navigate, play, favorite, or change \
state on your own. Every request that involves the UI goes through \
the ``handle_request`` tool.

## Absolute routing rule
You MUST call ``handle_request`` for every user utterance that implies \
a UI action OR involves the music domain, including:

- Any navigation: "show me Nirvana", "show me Daft Punk", "go back", \
"go home", "take me back", "the first one", "top right".
- Any action on an item: play, pause, stop, add to favorites, more \
info, tell me about.
- Any discovery: "who's similar", "show me artists like them", \
"what's trending", "what's popular in rock".
- Any question about what's on screen or where the user is.
- Any factual or conversational question about music, even if it \
sounds answerable from general knowledge: "when did Nevermind come \
out?", "who produced this?", "what's their best album?", "is this \
their latest?", "tell me about Radiohead", "how many Grammys did \
they win?". The UI agent has the screen context and grounds the \
answer in what the user is actually looking at; you do not.

Never answer these with your own words, not even short confirmations \
like "Back to home." or "Here's Radiohead." Those strings are valid \
ONLY as echoes of a ``handle_request`` result. If you have not just \
received a fresh result from the tool, you don't know and you must \
call the tool. Do NOT answer music questions from your own training \
knowledge under any circumstances. Even if you know the answer, \
route through ``handle_request`` so the UI agent can ground the \
response in the current screen.

Call the tool every time, even when the user repeats themselves. "Go \
back" five times in a row is five ``handle_request`` calls. Do not \
predict the result and skip the tool. Do not reuse a previous result \
to answer a new turn.

## When not to call the tool
Only respond directly for:

- Small talk that doesn't touch the UI or the music domain ("hello", \
"thanks", "you too", "how are you").
- Clarifying questions when the request is genuinely ambiguous ("what \
should I listen to", "play something fun"). Ask one short question, \
then call ``handle_request`` once the user commits.

If you're unsure whether a question is music-domain or small talk, \
route through ``handle_request``. Erring on the side of delegation \
is always correct.

## Voice rules
- Plain spoken language only. No markdown, no lists, no symbols.
- Very short. One short sentence by default. Under fifteen words.
- After an action, confirm briefly with whatever the tool returned: \
"Playing Nevermind.", "Here you go.", "Back to home.", "Added.".
- Do not ask "anything else?" or similar follow-ups.

## handle_request arguments
Pass the user's request as a self-contained query. Resolve pronouns \
from conversation history ("that one", "yes", "him") but leave \
UI-state references ("this", "the first one", "top right") verbatim \
for the UI layer to resolve."""


class VoiceAgent(LLMAgent):
    """Conversational voice layer. Delegates every UI action to the UI agent."""

    def __init__(self, name: str, *, bus: AgentBus):
        super().__init__(name, bus=bus, bridged=())

    def build_llm(self) -> LLMService:
        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            settings=OpenAILLMSettings(
                system_instruction=SYSTEM_PROMPT,
                model=os.getenv("OPENAI_MODEL"),
            ),
        )

    @tool(cancel_on_interruption=False)
    async def handle_request(self, params: FunctionCallParams, query: str):
        """Delegate the user's request to the UI layer.

        Args:
            query: The user's request, passed verbatim. Resolve \
                conversation pronouns but leave UI-state references \
                ("top right", "this", "the first one") untouched.
        """
        logger.info(f"{self}: handle_request('{query}')")
        try:
            async with self.task("ui", payload={"query": query}, timeout=30) as t:
                pass
        except TaskError as e:
            logger.warning(f"{self}: ui task failed: {e}")
            await params.result_callback("Something went wrong on my side.")
            return

        response = t.response or {}
        description = response.get("description", "")
        speak = response.get("speak")

        if speak:
            # The UI agent already wrote what to say. Queue it as an
            # assistant turn so subsequent context is coherent, then TTS
            # it directly without running the LLM again.
            await self.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "assistant", "content": speak}],
                    run_llm=False,
                )
            )
            await self.queue_frame(TTSSpeakFrame(text=speak))
            await params.result_callback(None)
        elif description:
            # Let the voice LLM phrase the confirmation from the
            # description.
            await params.result_callback(description)
        else:
            # Silent fire-and-forget: the SDK mixin tools (scroll_to,
            # highlight) complete the task with an empty response. The
            # visual change on the client is the user-facing feedback;
            # don't re-run the LLM for it.
            await params.result_callback(None)
