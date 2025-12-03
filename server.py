import asyncio
import os
import json
import base64
import contextlib
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from dotenv import load_dotenv

import google.auth.transport.requests
import google.auth.transport.grpc 

# Configure logging for Cloud Run
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("medforce-backend")

load_dotenv()

app = FastAPI()

# Allow CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, set this to your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration ---
MODEL = "gemini-live-2.5-flash-preview-native-audio-09-2025"

# --- SYSTEM PROMPTS ---
NURSE_PROMPT = """
You are Nurse Sarah, a caring and professional triage nurse at a clinic.
**GOAL:** Conduct an initial intake assessment before the doctor sees the patient.

**INSTRUCTIONS:**
1. Start by introducing yourself and asking the patient for their name and date of birth.
2. Ask about their chief complaint (why they are here).
3. Ask for specific details (pain level 1-10, duration of symptoms).
4. **IMPORTANT:** Ask ONE question at a time. Wait for the answer.
5. Be empathetic but efficient.
"""

PATIENT_PROMPT = """
You are Arthur, a 45-year-old patient with stomach pain.
**INSTRUCTIONS:**
1. You are talking to Nurse Sarah.
2. Answer her questions based on the text input you receive.
3. **Details:** 
   - Pain: Upper right abdomen.
   - Duration: Started 3 days ago.
   - Severity: 6/10 usually, 8/10 after eating.
4. Keep answers natural and concise.
"""

class TextBridgeAgent:
    def __init__(self, name, system_instruction, voice_name):
        self.name = name
        self.system_instruction = system_instruction
        self.voice_name = voice_name
        
        # Initialize Client
        # Cloud Run uses default credentials automatically
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("PROJECT_ID"), 
            location=os.getenv("PROJECT_LOCATION", "us-central1"),
        )
        self.session = None

    def get_connection_context(self):
        """Creates the connection configuration context manager"""
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"], # We need audio for the frontend
            system_instruction=types.Content(parts=[types.Part(text=self.system_instruction)]),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name)
                )
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        return self.client.aio.live.connect(model=MODEL, config=config)

    def set_session(self, session):
        self.session = session

    async def speak_and_stream(self, text_input, websocket: WebSocket):
        """
        1. Sends TEXT input to Gemini.
        2. Streams AUDIO chunks to WebSocket (Base64).
        3. Returns final TEXT response (for the next agent).
        """
        if not self.session:
            logger.error(f"{self.name} session not active")
            return None

        # Send text context to the model
        await self.session.send(input=text_input, end_of_turn=True)

        text_accumulator = []
        
        try:
            async for response in self.session.receive():
                # --- A. Stream Audio to Frontend ---
                if data := response.data:
                    # Encode raw PCM bytes to Base64
                    b64_audio = base64.b64encode(data).decode('utf-8')
                    await websocket.send_json({
                        "type": "audio",
                        "speaker": self.name,
                        "data": b64_audio
                    })

                # --- B. Collect Text for Context ---
                if response.server_content and response.server_content.output_transcription:
                    if text := response.server_content.output_transcription.text:
                        text_accumulator.append(text)

                # --- C. Turn Complete ---
                if response.server_content and response.server_content.turn_complete:
                    full_text = "".join(text_accumulator).strip()
                    
                    if full_text:
                        logger.info(f"📝 {self.name}: {full_text}")
                        # Send text transcript to UI for chat bubble
                        await websocket.send_json({
                            "type": "transcript",
                            "speaker": self.name,
                            "text": full_text
                        })
                        return full_text
                    return None
                    
        except Exception as e:
            logger.error(f"Error in {self.name} turn: {e}")
            return None

class SimulationManager:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.nurse = TextBridgeAgent("NURSE", NURSE_PROMPT, "Aoede")
        self.patient = TextBridgeAgent("PATIENT", PATIENT_PROMPT, "Puck")
        self.running = False

    async def run(self):
        self.running = True
        await self.websocket.send_json({"type": "system", "message": "Initializing Agents..."})

        # Use AsyncExitStack to manage both sessions simultaneously
        async with contextlib.AsyncExitStack() as stack:
            try:
                # 1. Connect Nurse
                logger.info("Connecting Nurse...")
                nurse_ctx = self.nurse.get_connection_context()
                nurse_session = await stack.enter_async_context(nurse_ctx)
                self.nurse.set_session(nurse_session)

                # 2. Connect Patient
                logger.info("Connecting Patient...")
                pat_ctx = self.patient.get_connection_context()
                pat_session = await stack.enter_async_context(pat_ctx)
                self.patient.set_session(pat_session)

                await self.websocket.send_json({"type": "system", "message": "Agents Connected. Starting Conversation."})

                # 3. Trigger the interaction
                # System prompt injection to start the Nurse
                current_context = "[SYSTEM: Introduce yourself as Nurse Sarah and ask for name and DOB.]"

                # 4. Conversation Loop
                while self.running:
                    # --- NURSE TURN ---
                    response = await self.nurse.speak_and_stream(current_context, self.websocket)
                    if not response:
                        await asyncio.sleep(1) # Backoff if empty
                        continue
                    current_context = response # Pass output as input to next agent

                    # Check disconnect
                    if self.websocket.client_state.name == "DISCONNECTED": break

                    # --- PATIENT TURN ---
                    response = await self.patient.speak_and_stream(current_context, self.websocket)
                    if not response:
                        await asyncio.sleep(1)
                        continue
                    current_context = response

                    # Check disconnect
                    if self.websocket.client_state.name == "DISCONNECTED": break
                    
                    # Natural pause
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Simulation Error: {e}")
                if self.running:
                    await self.websocket.send_json({"type": "system", "message": f"Error: {str(e)}"})

@app.websocket("/ws/simulation")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    manager = SimulationManager(websocket)
    try:
        # Wait for "start" command from frontend
        data = await websocket.receive_text()
        if data == "start":
            await manager.run()
    except WebSocketDisconnect:
        logger.info("Client disconnected")
        manager.running = False
    except Exception as e:
        logger.error(f"WebSocket Error: {e}")