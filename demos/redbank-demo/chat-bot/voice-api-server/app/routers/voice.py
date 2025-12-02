# Copyright 2025 IBM, Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from fastapi import APIRouter, File, UploadFile, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import base64
from ..services.whisper_svc import WhisperService
from ..services.responses_svc import ResponseService
from ..services.tts_svc import TTSService
from ..deps import get_logger
from ..config import settings
from .common import start_session_helper, chat_helper, clear_conversation_helper

router = APIRouter(prefix="/api/voice", tags=["voice"])

_whisper = None
_response = None
_tts = None


def _get_whisper():
    global _whisper
    if _whisper is None:
        _whisper = WhisperService(settings.whisper_model, settings.whisper_url)
    return _whisper


def _get_response():
    global _response
    if _response is None:
        _response = ResponseService(
            settings.llamastack_url,
            settings.inference_model,
            settings.vector_store_name,
            settings.mcp_url,
        )
    return _response


def _get_tts():
    global _tts
    if _tts is None:
        _tts = TTSService(settings.tts_url, settings.tts_voice)
    return _tts


@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...), logger=Depends(get_logger)):
    try:
        audio = await file.read()
        filename = file.filename or "audio.wav"  # Default to .wav if no filename
        logger.info(f"Received audio file: {filename}, size: {len(audio)} bytes")
        text, dur = _get_whisper().transcribe(audio, filename)
        logger.info(f"Transcribed {len(audio)} bytes to {len(text)} chars (dur≈{dur}s)")
        return {"text": text, "duration": dur}
    except Exception as e:
        logger.error(f"Transcription error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/complete")
async def complete(file: UploadFile = File(...), logger=Depends(get_logger)):
    audio = await file.read()
    filename = file.filename or "audio.wav"  # Default to .wav if no filename
    logger.info(f"Processing audio file: {filename}, size: {len(audio)} bytes")
    text, _ = _get_whisper().transcribe(audio, filename)

    # Clear conversation history before each call to ensure fresh query
    # This matches the UI behavior where each call is independent
    response_service = _get_response()
    response_service.clear_conversation()
    logger.info("Cleared conversation history for fresh query")

    agent_resp = response_service.invoke(text, settings.model_instructions)
    response_text = (
        agent_resp.get("output") or agent_resp.get("text") or str(agent_resp)
    )

    wav = _get_tts().synthesize(response_text)

    return JSONResponse(
        {
            "transcript": text,
            "agent_text": response_text,
            "wav_base64": base64.b64encode(wav).decode("ascii"),
        }
    )


@router.post("/speak")
async def speak(text: str):
    wav = _get_tts().synthesize(text)
    return StreamingResponse(iter([wav]), media_type="audio/wav")


@router.post("/session/start")
async def start_session(logger=Depends(get_logger)):
    """Start a new agent session"""
    return await start_session_helper(_get_response(), logger)


@router.post("/chat")
async def chat_with_agent(text: str, logger=Depends(get_logger)):
    """Chat with the agent using text input (for testing conversation continuity)"""
    return await chat_helper(text, _get_response(), logger)


@router.post("/conversation/clear")
async def clear_conversation(logger=Depends(get_logger)):
    """Clear the conversation history"""
    return await clear_conversation_helper(_get_response(), logger)
