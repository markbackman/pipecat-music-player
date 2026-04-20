#
# Copyright (c) 2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Voice + UI separation-of-concerns example: a voice-driven music player.

A ``VoiceAgent`` handles the conversation and a ``UIAgent`` owns the
navigation stack and screen state. The voice agent delegates every UI
request to the UI agent, which picks the right action with its own LLM
and emits ``RTVIServerMessageFrame`` updates to the client. Client
clicks flow back to the UI agent via a custom bus message and bypass
the LLM for deterministic, low-latency updates.

Architecture:

    MusicAgent (transport + BusBridge + RTVI client-message listener)
      ├── VoiceAgent (LLM, bridged)
      │     └── @tool handle_request(query)
      │           └── request_task("ui")
      └── UIAgent (LLM, not bridged)
            ├── tools: navigate_to_artist, select_item, play,
            │          show_info, add_to_favorites, go_back,
            │          go_home, describe_screen
            └── on_bus_message: dispatches ui_context click events

Run the server from this directory:

    uv run music_agent.py

Then open http://localhost:5173 (the Vite client in ``../client/``) to
talk to the bot.

Requirements:
- OPENAI_API_KEY
- DEEPGRAM_API_KEY
- CARTESIA_API_KEY
- DAILY_API_KEY (for Daily transport)
"""

import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService, CartesiaTTSSettings
from pipecat.services.soniox.stt import SonioxSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams
from pipecat_subagents.agents import BaseAgent, LLMAgentActivationArgs, agent_ready
from pipecat_subagents.bus import BusBridgeProcessor
from pipecat_subagents.runner import AgentRunner
from pipecat_subagents.types import AgentReadyData

from catalog_agent import CatalogAgent
from messages import BusUIContextMessage
from ui_agent import UIAgent
from voice_agent import VoiceAgent

load_dotenv(override=True)


class MusicAgent(BaseAgent):
    """Root agent: owns the transport and bridges frames to the voice agent."""

    def __init__(self, name: str, *, bus, transport: BaseTransport):
        super().__init__(name, bus=bus)
        self._transport = transport

    async def on_ready(self) -> None:
        await super().on_ready()

        # Forward UI click events from the RTVI client into the bus so
        # the UI agent can react without the voice agent mediating.
        @self.pipeline_task.rtvi.event_handler("on_client_message")
        async def on_client_message(rtvi, msg):
            if msg.type != "ui_context":
                return
            await self._bus.send(
                BusUIContextMessage(
                    source=self.name,
                    target="ui",
                    data=msg.data,
                )
            )

    @agent_ready(name="voice")
    async def on_voice_ready(self, data: AgentReadyData) -> None:
        await self.activate_agent(
            "voice",
            args=LLMAgentActivationArgs(
                messages=[
                    {
                        "role": "developer",
                        "content": (
                            "Greet the user. Welcome them to the voice music "
                            "player and mention they can ask to see any artist, "
                            "play a track, or get more info."
                        ),
                    }
                ],
            ),
        )

    def build_pipeline_task(self, pipeline: Pipeline) -> PipelineTask:
        return PipelineTask(
            pipeline,
            enable_rtvi=True,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )

    async def build_pipeline(self) -> Pipeline:
        stt = SonioxSTTService(
            api_key=os.getenv("SONIOX_API_KEY"),
            settings=SonioxSTTService.Settings(
                language_hints=[Language.EN],
                language_hints_strict=True,
            ),
        )
        tts = CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            settings=CartesiaTTSSettings(
                voice=os.getenv("CARTESIA_VOICE_ID"),
            ),
        )

        context = LLMContext()
        context_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        )

        bridge = BusBridgeProcessor(
            bus=self.bus,
            agent_name=self.name,
            name=f"{self.name}::BusBridge",
        )

        @self._transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info("Client connected")
            voice = VoiceAgent("voice", bus=self.bus)
            ui = UIAgent("ui", bus=self.bus)
            await self.add_agent(voice)
            await self.add_agent(ui)

        @self._transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected")
            await self.cancel()

        return Pipeline(
            [
                self._transport.input(),
                stt,
                context_aggregator.user(),
                bridge,
                tts,
                self._transport.output(),
                context_aggregator.assistant(),
            ]
        )


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    runner = AgentRunner(handle_sigint=runner_args.handle_sigint)
    # CatalogAgent is a peer of MusicAgent so its Deezer warm-up starts
    # concurrently with transport setup, not inside a per-client handler.
    catalog = CatalogAgent("catalog", bus=runner.bus)
    music = MusicAgent("music", bus=runner.bus, transport=transport)
    await runner.add_agent(catalog)
    await runner.add_agent(music)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Pipecat Cloud Client entry point."""

    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    transport_params = {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_in_filter=krisp_filter,
            audio_out_enabled=True,
        ),
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_in_filter=krisp_filter,
            audio_out_enabled=True,
        ),
    }

    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
